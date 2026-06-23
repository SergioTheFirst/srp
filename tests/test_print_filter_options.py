"""Filter-option lists + filtered CSV export (printview Phase 6).

GET /api/v1/fleet/print/filter-options -> {devices[], printers[], ips[]} for the
selects. GET /api/v1/fleet/print/export.csv now honors the full PrintFilter and
adds ip + validation columns; string cells are defanged against CSV formula
injection (leading = + - @ get a ' prefix).
"""

from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient
from tests.conftest import envelope, healthy

pytestmark = pytest.mark.integration


def _pj(device: str, jobs: list) -> dict:
    return envelope(device, "print_jobs", {"jobs": jobs, "window_from": None})


def _ev(printer: str, pages: int, job_id: int, ts: str = "2026-06-10T10:00:00+00:00") -> dict:
    return {
        "job_id": job_id,
        "ts": ts,
        "printer": printer,
        "pages": pages,
        "size_bytes": 1000,
        "user_name": "x",
        "source": "events",
    }


def _counter(printer: str, pages: int, ts: str = "2026-06-10T11:00:00+00:00") -> dict:
    return {"job_id": None, "ts": ts, "printer": printer, "pages": pages, "source": "counter"}


def _hist_ports(ports: list) -> dict:
    payload = healthy("historical")
    payload["printer_ports"] = ports
    return payload


def _csv_rows(text: str) -> list:
    return list(csv.DictReader(io.StringIO(text)))


# --------------------------------------------------------------------------- #
# filter-options
# --------------------------------------------------------------------------- #
def test_filter_options_empty(client: TestClient) -> None:
    body = client.get("/api/v1/fleet/print/filter-options").json()
    assert body["devices"] == []
    assert body["printers"] == []
    assert body["ips"] == []


def test_filter_options_populated(client: TestClient) -> None:
    client.post(
        "/api/v1/ingest",
        json=envelope("pc-1", "historical", _hist_ports([{"name": "HP", "ip": "192.168.1.50"}])),
    )
    client.post("/api/v1/ingest", json=_pj("pc-1", [_ev("HP", 5, 1)]))
    client.post("/api/v1/ingest", json=_pj("pc-2", [_ev("Xerox", 3, 2)]))
    body = client.get("/api/v1/fleet/print/filter-options").json()
    devs = {d["device_id"] for d in body["devices"]}
    assert {"pc-1", "pc-2"} <= devs
    assert "HP" in body["printers"]
    assert "Xerox" in body["printers"]
    assert "192.168.1.50" in body["ips"]


def test_filter_options_scoped_to_period(client: TestClient) -> None:
    client.post(
        "/api/v1/ingest", json=_pj("pc-old", [_ev("Old", 1, 1, "2026-01-01T10:00:00+00:00")])
    )
    client.post(
        "/api/v1/ingest", json=_pj("pc-new", [_ev("New", 1, 2, "2026-06-20T10:00:00+00:00")])
    )
    body = client.get(
        "/api/v1/fleet/print/filter-options?date_from=2026-06-01&date_to=2026-06-30"
    ).json()
    printers = set(body["printers"])
    assert "New" in printers
    assert "Old" not in printers  # outside the window


# --------------------------------------------------------------------------- #
# CSV export
# --------------------------------------------------------------------------- #
def test_export_csv_has_ip_and_validation_columns(client: TestClient) -> None:
    client.post(
        "/api/v1/ingest",
        json=envelope("pc-1", "historical", _hist_ports([{"name": "HP", "ip": "192.168.1.50"}])),
    )
    client.post("/api/v1/ingest", json=_pj("pc-1", [_ev("HP", 4, 1), _counter("HP", 9)]))
    rows = _csv_rows(client.get("/api/v1/fleet/print/export.csv").text)
    assert "ip" in rows[0]
    assert "validation" in rows[0]
    by_source = {r["source"]: r for r in rows}
    assert by_source["events"]["validation"] == "точно"
    assert by_source["counter"]["validation"] == "оценка"
    assert by_source["events"]["ip"] == "192.168.1.50"


def test_export_csv_respects_printer_filter(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=_pj("pc-1", [_ev("HP", 5, 1), _ev("Xerox", 3, 2)]))
    rows = _csv_rows(client.get("/api/v1/fleet/print/export.csv?printer=HP").text)
    assert len(rows) == 1
    assert rows[0]["printer"] == "HP"


def test_export_csv_neutralizes_formula_injection(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=_pj("pc-1", [_ev("=cmd|'/c calc'!A0", 5, 1)]))
    rows = _csv_rows(client.get("/api/v1/fleet/print/export.csv").text)
    # leading '=' must be defanged with a single quote so spreadsheets treat it as text
    assert rows[0]["printer"].startswith("'=")


def test_export_csv_filename_is_structured(client: TestClient) -> None:
    r = client.get("/api/v1/fleet/print/export.csv?date_from=2026-01-01&date_to=2026-06-30")
    assert r.headers["content-disposition"] == (
        "attachment; filename=print_export_20260101_20260630.csv"
    )


def test_export_csv_filename_rejects_header_injection(client: TestClient) -> None:
    # A newline/extra-laden "date" must never reach the Content-Disposition header
    # (response header injection); the strict validator collapses it to 'all'.
    r = client.get("/api/v1/fleet/print/export.csv?date_from=2026-01-01%0AX-Evil:+1")
    cd = r.headers["content-disposition"]
    assert "\n" not in cd and "X-Evil" not in cd
    assert cd == "attachment; filename=print_export_all_all.csv"


def test_export_csv_filtered_matches_records_count(client: TestClient) -> None:
    client.post(
        "/api/v1/ingest",
        json=_pj("pc-1", [_ev("HP", 5, 1), _ev("HP", 6, 2), _ev("Xerox", 1, 3)]),
    )
    csv_rows = _csv_rows(client.get("/api/v1/fleet/print/export.csv?printer=HP").text)
    rec_total = client.get("/api/v1/fleet/print/records?printer=HP").json()["total"]
    assert len(csv_rows) == rec_total == 2
