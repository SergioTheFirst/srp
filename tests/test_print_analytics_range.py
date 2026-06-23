"""Legacy print analytics under an explicit date range (printview Phase 11 backend).

GET /api/v1/fleet/print/analytics now accepts date_from/date_to so the old
sections (daily/printers/users/departments) react to the shared range. The
legacy ?days= window keeps working unchanged when no range is supplied.
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


def test_analytics_date_range_filters_sections(client: TestClient) -> None:
    client.post(
        "/api/v1/ingest",
        json=_pj(
            "pc-1",
            [
                _ev("HP", 5, 1, "2026-06-01T10:00:00+00:00"),
                _ev("HP", 8, 2, "2026-06-20T10:00:00+00:00"),
            ],
        ),
    )
    body = client.get(
        "/api/v1/fleet/print/analytics?date_from=2026-06-15&date_to=2026-06-25"
    ).json()
    assert body["total_pages"] == 8
    dates = [d["date"] for d in body["daily"]]
    assert "2026-06-20" in dates
    assert "2026-06-01" not in dates


def test_analytics_days_mode_unchanged(client: TestClient) -> None:
    body = client.get("/api/v1/fleet/print/analytics?days=30").json()
    # legacy shape intact, including the period-over-period delta fields
    assert "total_pages" in body
    assert "prev_total_pages" in body
    assert "prev_total_jobs" in body
