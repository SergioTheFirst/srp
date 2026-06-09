"""Print tracking — DB, pipeline, API, and collector unit tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from tests.conftest import envelope

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pj_envelope(device_id: str, jobs: list) -> dict:
    return envelope(device_id, "print_jobs", {"jobs": jobs, "window_from": None})


def _job(printer: str = "HP LaserJet", pages: int = 2, user: str = "alice") -> dict:
    return {
        "job_id": 42,
        "ts": "2026-06-09T10:00:00+00:00",
        "printer": printer,
        "pages": pages,
        "size_bytes": 12000,
        "user_name": user,
    }


# ---------------------------------------------------------------------------
# Pipeline: print_jobs msg_type
# ---------------------------------------------------------------------------


def test_print_jobs_ingest_returns_200(client: TestClient) -> None:
    r = client.post("/api/v1/ingest", json=_pj_envelope("dev-1", [_job()]))
    assert r.status_code == 200
    body = r.json()
    assert body["msg_type"] == "print_jobs"
    # print_jobs must NOT trigger score recompute
    assert body["scores_updated"] is False


def test_print_jobs_empty_jobs_list_accepted(client: TestClient) -> None:
    r = client.post("/api/v1/ingest", json=_pj_envelope("dev-1", []))
    assert r.status_code == 200


def test_print_jobs_does_not_overwrite_scores(client: TestClient) -> None:
    """Ingesting print_jobs after inventory must leave scores intact."""
    client.post(
        "/api/v1/ingest",
        json=envelope(
            "dev-2",
            "inventory",
            {
                "hostname": "PC-PRINT",
                "manufacturer": "Dell",
                "model": "OptiPlex",
                "chassis": "desktop",
            },
        ),
    )
    before = client.get("/api/v1/devices/dev-2").json()
    client.post("/api/v1/ingest", json=_pj_envelope("dev-2", [_job()]))
    after = client.get("/api/v1/devices/dev-2").json()
    assert before.get("risk_exposure") == after.get("risk_exposure")


# ---------------------------------------------------------------------------
# API: /api/v1/devices/{id}/print
# ---------------------------------------------------------------------------


def test_device_print_returns_zero_when_no_jobs(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=envelope("dev-3", "inventory", {"hostname": "PC3"}))
    r = client.get("/api/v1/devices/dev-3/print?days=30")
    assert r.status_code == 200
    body = r.json()
    assert body["total_pages"] == 0
    assert body["total_jobs"] == 0
    assert body["printers"] == []


def test_device_print_404_for_unknown_device(client: TestClient) -> None:
    r = client.get("/api/v1/devices/no-such-device/print")
    assert r.status_code == 404


def test_device_print_counts_pages(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=envelope("dev-4", "inventory", {"hostname": "PC4"}))
    jobs = [
        {**_job(pages=5), "job_id": 101},
        {**_job(pages=3, printer="Brother MFC"), "job_id": 102},
    ]
    client.post("/api/v1/ingest", json=_pj_envelope("dev-4", jobs))
    r = client.get("/api/v1/devices/dev-4/print?days=30")
    assert r.status_code == 200
    body = r.json()
    assert body["total_pages"] == 8
    assert body["total_jobs"] == 2
    assert len(body["printers"]) == 2


# ---------------------------------------------------------------------------
# API: /api/v1/fleet/print
# ---------------------------------------------------------------------------


def test_fleet_print_empty(client: TestClient) -> None:
    r = client.get("/api/v1/fleet/print?days=30")
    assert r.status_code == 200
    body = r.json()
    assert body["total_pages"] == 0
    assert body["total_jobs"] == 0
    assert body["printer_count"] == 0


def test_fleet_print_aggregates_across_devices(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=_pj_envelope("dev-a", [_job(pages=10)]))
    client.post("/api/v1/ingest", json=_pj_envelope("dev-b", [_job(pages=7)]))
    r = client.get("/api/v1/fleet/print?days=30")
    body = r.json()
    assert body["total_pages"] == 17
    assert body["total_jobs"] == 2


# ---------------------------------------------------------------------------
# API: /api/v1/fleet/print/analytics
# ---------------------------------------------------------------------------


def test_fleet_print_analytics_shape(client: TestClient) -> None:
    client.post(
        "/api/v1/ingest",
        json=_pj_envelope("dev-c", [_job(printer="Xerox WorkCentre", pages=4, user="bob")]),
    )
    r = client.get("/api/v1/fleet/print/analytics?days=30")
    assert r.status_code == 200
    body = r.json()
    assert "daily" in body
    assert "printers" in body
    assert "users" in body
    assert "departments" in body
    assert body["total_pages"] == 4


def test_fleet_print_analytics_daily_has_date_and_pages(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=_pj_envelope("dev-d", [_job(pages=3)]))
    body = client.get("/api/v1/fleet/print/analytics?days=30").json()
    for row in body["daily"]:
        assert "date" in row
        assert "pages" in row


# ---------------------------------------------------------------------------
# API: /api/v1/fleet/print/export.csv
# ---------------------------------------------------------------------------


def test_fleet_print_export_csv_headers(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=_pj_envelope("dev-e", [_job(pages=2)]))
    r = client.get("/api/v1/fleet/print/export.csv?days=30")
    assert r.status_code == 200
    assert "text/csv" in r.headers.get("content-type", "")
    lines = r.text.splitlines()
    assert lines[0].startswith("ts,")
    assert "pages" in lines[0]
    assert len(lines) >= 2  # header + at least one row


def test_fleet_print_export_csv_empty_when_no_jobs(client: TestClient) -> None:
    r = client.get("/api/v1/fleet/print/export.csv?days=30")
    assert r.status_code == 200
    lines = r.text.strip().splitlines()
    assert len(lines) == 1  # header only


# ---------------------------------------------------------------------------
# API: /api/v1/devices/{id}/meta PATCH (department)
# ---------------------------------------------------------------------------


def test_patch_meta_sets_department(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=envelope("dev-f", "inventory", {"hostname": "PC-F"}))
    r = client.patch("/api/v1/devices/dev-f/meta", json={"department": "IT"})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_patch_meta_404_unknown_device(client: TestClient) -> None:
    r = client.patch("/api/v1/devices/ghost/meta", json={"department": "HR"})
    assert r.status_code == 404


def test_patch_meta_null_department_accepted(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=envelope("dev-g", "inventory", {"hostname": "PC-G"}))
    r = client.patch("/api/v1/devices/dev-g/meta", json={"department": None})
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Collector: _parse_job unit (no PowerShell)
# ---------------------------------------------------------------------------


def test_collector_parse_job_valid() -> None:
    from client.collectors.print_jobs import _parse_job

    raw = {
        "job_id": 1,
        "ts": "2026-06-09T10:00:00Z",
        "printer": "HP LaserJet",
        "pages": 4,
        "size_bytes": 8000,
        "user_name": "alice",
    }
    result = _parse_job(raw)
    assert result is not None
    assert result["pages"] == 4
    assert result["printer"] == "HP LaserJet"


def test_collector_parse_job_filters_virtual_printer() -> None:
    from client.collectors.print_jobs import _parse_job

    raw = {
        "job_id": 2,
        "ts": "2026-06-09T10:01:00Z",
        "printer": "Microsoft Print to PDF",
        "pages": 2,
        "size_bytes": 1000,
        "user_name": "alice",
    }
    assert _parse_job(raw) is None


def test_collector_parse_job_filters_zero_pages() -> None:
    from client.collectors.print_jobs import _parse_job

    raw = {
        "job_id": 3,
        "ts": "2026-06-09T10:02:00Z",
        "printer": "HP LaserJet",
        "pages": 0,
        "size_bytes": 0,
        "user_name": "bob",
    }
    assert _parse_job(raw) is None


def test_collector_parse_job_none_input() -> None:
    from client.collectors.print_jobs import _parse_job

    assert _parse_job(None) is None


def test_collector_safe_ts_valid() -> None:
    from client.collectors.print_jobs import _safe_ts

    assert _safe_ts("2026-06-09T10:00:00+00:00") == "2026-06-09T10:00:00+00:00"


def test_collector_safe_ts_rejects_injection() -> None:
    from client.collectors.print_jobs import _safe_ts

    assert _safe_ts("'; DROP TABLE--") == ""
    assert _safe_ts("2026-06-09 10:00:00; rm -rf") == ""
