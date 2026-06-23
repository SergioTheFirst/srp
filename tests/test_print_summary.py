"""Print summary metrics endpoint (printview Phase 3).

GET /api/v1/fleet/print/summary -> the 7 headline cards, honoring the filters.
Jobs are counted event-only (counter-mode rows add pages, never jobs).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from tests.conftest import envelope, healthy

pytestmark = pytest.mark.integration


def _pj(device: str, jobs: list) -> dict:
    return envelope(device, "print_jobs", {"jobs": jobs, "window_from": None})


def _ev(printer: str, pages: int, job_id: int, ts: str = "2026-06-09T10:00:00+00:00") -> dict:
    return {
        "job_id": job_id,
        "ts": ts,
        "printer": printer,
        "pages": pages,
        "size_bytes": 1000,
        "user_name": "x",
        "source": "events",
    }


def _counter(printer: str, pages: int, ts: str = "2026-06-09T10:00:00+00:00") -> dict:
    return {"job_id": None, "ts": ts, "printer": printer, "pages": pages, "source": "counter"}


def _hist_ports(ports: list) -> dict:
    payload = healthy("historical")
    payload["printer_ports"] = ports
    return payload


def test_summary_empty(client: TestClient) -> None:
    body = client.get("/api/v1/fleet/print/summary").json()
    assert body["total_pages"] == 0
    assert body["total_jobs"] == 0
    assert body["active_printers"] == 0
    assert body["active_computers"] == 0
    assert body["busiest_printer"] is None
    assert body["most_active_computer"] is None
    assert body["avg_pages_per_job"] == 0


def test_summary_counts_events(client: TestClient) -> None:
    client.post(
        "/api/v1/ingest",
        json=_pj("pc-1", [_ev("HP", 5, 1), _ev("HP", 3, 2), _ev("Xerox", 2, 3)]),
    )
    body = client.get("/api/v1/fleet/print/summary").json()
    assert body["total_pages"] == 10
    assert body["total_jobs"] == 3
    assert body["active_printers"] == 2
    assert body["active_computers"] == 1
    assert body["busiest_printer"]["name"] == "HP"
    assert body["busiest_printer"]["pages"] == 8
    assert body["most_active_computer"]["device_id"] == "pc-1"
    assert body["avg_pages_per_job"] == round(10 / 3, 1)


def test_summary_counter_pages_are_not_jobs(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=_pj("pc-2", [_ev("HP", 4, 10), _counter("HP", 20)]))
    body = client.get("/api/v1/fleet/print/summary").json()
    assert body["total_pages"] == 24  # 4 + 20 counter delta
    assert body["total_jobs"] == 1  # only the event-sourced job
    assert body["avg_pages_per_job"] == 4.0  # events pages / events jobs


def test_summary_busiest_and_most_active(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=_pj("pc-a", [_ev("P1", 10, 1)]))
    client.post("/api/v1/ingest", json=_pj("pc-b", [_ev("P2", 30, 2)]))
    body = client.get("/api/v1/fleet/print/summary").json()
    assert body["busiest_printer"]["name"] == "P2"
    assert body["most_active_computer"]["device_id"] == "pc-b"
    assert body["active_computers"] == 2


def test_summary_filter_by_device(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=_pj("pc-x", [_ev("P", 10, 1)]))
    client.post("/api/v1/ingest", json=_pj("pc-y", [_ev("P", 5, 2)]))
    body = client.get("/api/v1/fleet/print/summary?device=pc-x").json()
    assert body["total_pages"] == 10
    assert body["active_computers"] == 1


def test_summary_filter_by_printer(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=_pj("pc-1", [_ev("HP", 7, 1), _ev("Xerox", 3, 2)]))
    body = client.get("/api/v1/fleet/print/summary?printer=HP").json()
    assert body["total_pages"] == 7
    assert body["active_printers"] == 1


def test_summary_filter_by_date_range(client: TestClient) -> None:
    client.post(
        "/api/v1/ingest",
        json=_pj(
            "pc-1",
            [
                _ev("HP", 5, 1, ts="2026-06-01T10:00:00+00:00"),
                _ev("HP", 8, 2, ts="2026-06-20T10:00:00+00:00"),
            ],
        ),
    )
    body = client.get("/api/v1/fleet/print/summary?date_from=2026-06-15&date_to=2026-06-25").json()
    assert body["total_pages"] == 8
    assert body["total_jobs"] == 1


def test_summary_busiest_printer_has_resolved_ip(client: TestClient) -> None:
    client.post(
        "/api/v1/ingest",
        json=envelope("pc-ip", "historical", _hist_ports([{"name": "HP", "ip": "192.168.1.50"}])),
    )
    client.post("/api/v1/ingest", json=_pj("pc-ip", [_ev("HP", 9, 1)]))
    body = client.get("/api/v1/fleet/print/summary").json()
    assert body["busiest_printer"]["name"] == "HP"
    assert body["busiest_printer"]["ip"] == "192.168.1.50"


def test_summary_filter_by_ip(client: TestClient) -> None:
    client.post(
        "/api/v1/ingest",
        json=envelope("pc-ip2", "historical", _hist_ports([{"name": "HP", "ip": "192.168.1.50"}])),
    )
    client.post("/api/v1/ingest", json=_pj("pc-ip2", [_ev("HP", 9, 1), _ev("Other", 4, 2)]))
    body = client.get("/api/v1/fleet/print/summary?ip=192.168.1.50").json()
    assert body["total_pages"] == 9  # only the HP queue resolves to that IP
    assert body["active_printers"] == 1
