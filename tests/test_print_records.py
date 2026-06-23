"""Detailed print records: server pagination/sort/search (printview Phase 5).

GET /api/v1/fleet/print/records -> {page, page_size, total, rows[]}. Each row
carries machine ``source`` (events/counter) plus localized ``source_label`` /
``validation`` / ``validation_color`` (events=точно/good, counter=оценка/warn).
Sort keys are whitelisted; search matches hostname/printer/ip.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from tests.conftest import envelope

pytestmark = pytest.mark.integration


def _pj(device: str, jobs: list) -> dict:
    return envelope(device, "print_jobs", {"jobs": jobs, "window_from": None})


def _ev(printer: str, pages: int, job_id: int, ts: str) -> dict:
    return {
        "job_id": job_id,
        "ts": ts,
        "printer": printer,
        "pages": pages,
        "size_bytes": 1000,
        "user_name": "x",
        "source": "events",
    }


def _counter(printer: str, pages: int, ts: str) -> dict:
    return {"job_id": None, "ts": ts, "printer": printer, "pages": pages, "source": "counter"}


def test_records_empty(client: TestClient) -> None:
    body = client.get("/api/v1/fleet/print/records").json()
    assert body["total"] == 0
    assert body["rows"] == []
    assert body["page"] == 1


def test_records_pagination(client: TestClient) -> None:
    jobs = [_ev("HP", 1, i, f"2026-06-{(i % 27) + 1:02d}T10:00:00+00:00") for i in range(1, 26)]
    client.post("/api/v1/ingest", json=_pj("pc-1", jobs))
    p1 = client.get("/api/v1/fleet/print/records?page=1&page_size=10").json()
    assert p1["total"] == 25
    assert p1["page_size"] == 10
    assert len(p1["rows"]) == 10
    p3 = client.get("/api/v1/fleet/print/records?page=3&page_size=10").json()
    assert len(p3["rows"]) == 5  # last page remainder


def test_records_page_size_clamped(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=_pj("pc-1", [_ev("HP", 1, 1, "2026-06-10T10:00:00+00:00")]))
    body = client.get("/api/v1/fleet/print/records?page_size=9999").json()
    assert body["page_size"] == 200  # capped


def test_records_validation_labels(client: TestClient) -> None:
    client.post(
        "/api/v1/ingest",
        json=_pj(
            "pc-1",
            [
                _ev("HP", 4, 1, "2026-06-10T10:00:00+00:00"),
                _counter("HP", 9, "2026-06-10T11:00:00+00:00"),
            ],
        ),
    )
    rows = client.get("/api/v1/fleet/print/records").json()["rows"]
    by_source = {r["source"]: r for r in rows}
    assert by_source["events"]["validation"] == "точно"
    assert by_source["events"]["validation_color"] == "good"
    assert by_source["events"]["source_label"] == "журнал"
    assert by_source["counter"]["validation"] == "оценка"
    assert by_source["counter"]["validation_color"] == "warn"
    assert by_source["counter"]["source_label"] == "счётчик"


def test_records_sort_by_pages(client: TestClient) -> None:
    client.post(
        "/api/v1/ingest",
        json=_pj(
            "pc-1",
            [
                _ev("A", 5, 1, "2026-06-10T10:00:00+00:00"),
                _ev("B", 20, 2, "2026-06-10T11:00:00+00:00"),
                _ev("C", 1, 3, "2026-06-10T12:00:00+00:00"),
            ],
        ),
    )
    rows = client.get("/api/v1/fleet/print/records?sort=pages&dir=desc").json()["rows"]
    assert [r["pages"] for r in rows] == [20, 5, 1]
    rows_asc = client.get("/api/v1/fleet/print/records?sort=pages&dir=asc").json()["rows"]
    assert [r["pages"] for r in rows_asc] == [1, 5, 20]


def test_records_search_by_printer(client: TestClient) -> None:
    client.post(
        "/api/v1/ingest",
        json=_pj(
            "pc-1",
            [
                _ev("HP LaserJet", 5, 1, "2026-06-10T10:00:00+00:00"),
                _ev("Xerox", 3, 2, "2026-06-10T11:00:00+00:00"),
            ],
        ),
    )
    body = client.get("/api/v1/fleet/print/records?q=laser").json()
    assert body["total"] == 1
    assert body["rows"][0]["printer"] == "HP LaserJet"


def test_records_unknown_sort_falls_back(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=_pj("pc-1", [_ev("HP", 5, 1, "2026-06-10T10:00:00+00:00")]))
    # An injection-ish sort key must neither break nor leak -> silently defaults to ts.
    body = client.get("/api/v1/fleet/print/records?sort=pages;DROP+TABLE").json()
    assert body["total"] == 1


def test_records_search_treats_wildcards_literally(client: TestClient) -> None:
    client.post(
        "/api/v1/ingest",
        json=_pj(
            "pc-1",
            [
                _ev("HP", 5, 1, "2026-06-10T10:00:00+00:00"),
                _ev("Xerox", 3, 2, "2026-06-10T11:00:00+00:00"),
            ],
        ),
    )
    # A lone '%' must match literally (no printer contains it), not act as match-all.
    body = client.get("/api/v1/fleet/print/records?q=%25").json()
    assert body["total"] == 0


def test_records_unresolved_ip_is_null(client: TestClient) -> None:
    client.post("/api/v1/ingest", json=_pj("pc-1", [_ev("HP", 5, 1, "2026-06-10T10:00:00+00:00")]))
    body = client.get("/api/v1/fleet/print/records").json()
    assert body["rows"][0]["ip"] is None
