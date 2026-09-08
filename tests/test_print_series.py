"""Print time-series with auto bucket detail (printview Phase 4).

GET /api/v1/fleet/print/series -> {granularity, buckets, series[], others, pair_count}.
Series = (computer -> printer) pairs; pairs beyond max_series fold into «прочее»;
every series' points align to a shared bucket axis (0 in empty buckets). Bucket
granularity auto-scales with the span: hour <=2d, day <=45d, week <=180d, else month.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tests.conftest import envelope, healthy

pytestmark = pytest.mark.integration


def _pj(device: str, jobs: list) -> dict:
    return envelope(device, "print_jobs", {"jobs": jobs, "window_from": None})


def _local_ts(local_naive: datetime) -> str:
    """UTC ISO ts for a naive *local* wall-clock datetime.

    Bucket expressions apply SQLite's 'localtime' modifier to the stored UTC
    ts (KodSR TZ finding), so fixtures must be anchored to local wall-clock
    time -- not literal UTC strings -- to stay correct on any machine TZ.
    Mirrors how the code itself derives UTC cutoffs from a local date
    (``_date_cutoff_utc``: naive ``datetime.astimezone()`` = local time)."""
    return local_naive.astimezone().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


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


def _hist_ports(ports: list) -> dict:
    payload = healthy("historical")
    payload["printer_ports"] = ports
    return payload


# --------------------------------------------------------------------------- #
# Unit: granularity thresholds
# --------------------------------------------------------------------------- #
def test_gran_expr_bucket_boundaries_use_localtime() -> None:
    """TZ-proof: on a UTC CI box 'localtime' is a no-op, so the fixture-based
    TZ tests above pass either way -- assert the SQL itself carries the
    modifier, or a regression that drops it would go unnoticed on UTC CI."""
    from server.db import _GRAN_EXPR

    assert all("'localtime'" in expr for expr in _GRAN_EXPR.values())


def test_auto_granularity_thresholds() -> None:
    from server.db import _auto_granularity

    base = datetime(2026, 1, 1)

    def d(n: int) -> str:
        return (base + timedelta(days=n)).strftime("%Y-%m-%d")

    assert _auto_granularity(d(0), d(0)) == "hour"  # same day
    assert _auto_granularity(d(0), d(2)) == "hour"  # 2 days
    assert _auto_granularity(d(0), d(3)) == "day"  # 3 days
    assert _auto_granularity(d(0), d(45)) == "day"  # 45 days
    assert _auto_granularity(d(0), d(46)) == "week"  # 46 days
    assert _auto_granularity(d(0), d(180)) == "week"  # 180 days
    assert _auto_granularity(d(0), d(181)) == "month"  # 181 days
    assert _auto_granularity(None, None) == "day"  # unknown span -> day default


# --------------------------------------------------------------------------- #
# Integration: endpoint behavior
# --------------------------------------------------------------------------- #
def test_series_empty(client: TestClient) -> None:
    body = client.get("/api/v1/fleet/print/series").json()
    assert body["buckets"] == []
    assert body["series"] == []
    assert body["others"] is None
    assert body["pair_count"] == 0


def test_series_hour_granularity_for_one_day(client: TestClient) -> None:
    h9 = datetime(2026, 6, 10, 9, 0, 0)
    h11 = datetime(2026, 6, 10, 11, 0, 0)
    client.post(
        "/api/v1/ingest",
        json=_pj(
            "pc-1",
            [
                _ev("HP", 5, 1, _local_ts(h9)),
                _ev("HP", 3, 2, _local_ts(h11)),
            ],
        ),
    )
    body = client.get("/api/v1/fleet/print/series?date_from=2026-06-10&date_to=2026-06-10").json()
    assert body["granularity"] == "hour"
    assert body["buckets"] == [h9.strftime("%Y-%m-%d %H:00"), h11.strftime("%Y-%m-%d %H:00")]
    s = body["series"][0]
    assert s["device_id"] == "pc-1"
    assert s["printer"] == "HP"
    assert s["points"] == [5, 3]


def test_series_month_granularity_for_year(client: TestClient) -> None:
    client.post(
        "/api/v1/ingest",
        json=_pj(
            "pc-1",
            [
                _ev("HP", 5, 1, "2026-01-10T09:00:00+00:00"),
                _ev("HP", 7, 2, "2026-03-10T09:00:00+00:00"),
            ],
        ),
    )
    body = client.get("/api/v1/fleet/print/series?date_from=2026-01-01&date_to=2026-12-31").json()
    assert body["granularity"] == "month"
    assert "2026-01" in body["buckets"]
    assert "2026-03" in body["buckets"]


def test_series_points_align_to_shared_axis(client: TestClient) -> None:
    # Two printers print in different hour buckets -> each series is padded with a
    # 0 in the foreign bucket so all points line up on the shared axis.
    client.post(
        "/api/v1/ingest",
        json=_pj(
            "pc-1",
            [
                _ev("HP", 5, 1, "2026-06-10T09:00:00+00:00"),
                _ev("Xerox", 4, 2, "2026-06-11T09:00:00+00:00"),
            ],
        ),
    )
    body = client.get("/api/v1/fleet/print/series?date_from=2026-06-10&date_to=2026-06-11").json()
    buckets = body["buckets"]
    assert len(buckets) == 2
    for s in body["series"]:
        assert len(s["points"]) == len(buckets)
    flat = sorted(p for s in body["series"] for p in s["points"])
    assert flat == [0, 0, 4, 5]


def test_series_collapses_extra_pairs_into_others(client: TestClient) -> None:
    jobs = [_ev(f"P{i}", i + 1, i + 1, "2026-06-10T09:00:00+00:00") for i in range(5)]
    client.post("/api/v1/ingest", json=_pj("pc-1", jobs))
    body = client.get(
        "/api/v1/fleet/print/series?date_from=2026-06-10&date_to=2026-06-10&max_series=3"
    ).json()
    assert len(body["series"]) == 3
    assert body["pair_count"] == 5
    assert body["others"] is not None
    assert body["others"]["label"] == "прочее"
    # Top 3 = P4(5),P3(4),P2(3); others = P1(2)+P0(1) = 3 in the single bucket.
    assert sum(body["others"]["points"]) == 3


def test_series_explicit_granularity_overrides_auto(client: TestClient) -> None:
    client.post(
        "/api/v1/ingest",
        json=_pj("pc-1", [_ev("HP", 5, 1, "2026-06-10T09:00:00+00:00")]),
    )
    body = client.get(
        "/api/v1/fleet/print/series?date_from=2026-06-10&date_to=2026-06-10&granularity=month"
    ).json()
    assert body["granularity"] == "month"
    assert body["buckets"] == ["2026-06"]


def test_series_label_and_resolved_ip(client: TestClient) -> None:
    client.post(
        "/api/v1/ingest",
        json=envelope("pc-1", "historical", _hist_ports([{"name": "HP", "ip": "192.168.1.50"}])),
    )
    client.post(
        "/api/v1/ingest",
        json=_pj("pc-1", [_ev("HP", 5, 1, "2026-06-10T09:00:00+00:00")]),
    )
    body = client.get("/api/v1/fleet/print/series?date_from=2026-06-10&date_to=2026-06-10").json()
    s = body["series"][0]
    assert s["ip"] == "192.168.1.50"
    assert s["printer"] == "HP"
    assert "→" in s["label"]


# --------------------------------------------------------------------------- #
# Week buckets by Monday, not %W; buckets on local calendar days (KodSR L13 + TZ)
# --------------------------------------------------------------------------- #
def test_week_bucket_by_monday_spans_year_boundary(client: TestClient) -> None:
    # 2025-12-29 = Monday, 2026-01-04 = Sunday of the same ISO week -- %W would
    # split these into "2025-52" and "2026-00" (resets at Jan 1); the fixed
    # week bucket must fold both into the single Monday-anchored date.
    mon = datetime(2025, 12, 29, 12, 0, 0)
    sun = datetime(2026, 1, 4, 12, 0, 0)
    client.post(
        "/api/v1/ingest",
        json=_pj(
            "pc-1",
            [
                _ev("HP", 5, 1, _local_ts(mon)),
                _ev("HP", 3, 2, _local_ts(sun)),
            ],
        ),
    )
    body = client.get("/api/v1/fleet/print/series?date_from=2025-12-01&date_to=2026-01-31").json()
    assert body["granularity"] == "week"  # span=61 days -> auto week (46-180)
    assert body["buckets"] == ["2025-12-29"]
    assert body["series"][0]["points"] == [8]


def test_bucket_uses_local_calendar_day_not_utc(client: TestClient) -> None:
    # A job printed just after local midnight must bucket into TODAY's local
    # date. On a positive-UTC-offset machine the stored UTC ts is still
    # "yesterday" -- without the 'localtime' modifier the bucket would be
    # off by one day. Expected date is derived the same way the code
    # computes it, so this holds on any machine TZ.
    local_after_midnight = (
        datetime.now().astimezone().replace(hour=0, minute=30, second=0, microsecond=0)
    )
    today = local_after_midnight.strftime("%Y-%m-%d")
    client.post(
        "/api/v1/ingest",
        json=_pj("pc-1", [_ev("HP", 4, 1, _local_ts(local_after_midnight.replace(tzinfo=None)))]),
    )
    body = client.get(
        f"/api/v1/fleet/print/series?date_from={today}&date_to={today}&granularity=day"
    ).json()
    assert body["buckets"] == [today]
    assert body["series"][0]["points"] == [4]
