"""W0.2 server-stamped time + clock-drift.

The server must not trust the client clock: Windows event/sample time depends on
the machine's own system clock, so staleness, trends, and event windows are
anchored to a server-stamped ``received_at``. ``ts`` is retained as the
client-reported (observed) time for compatibility. A large ``|received_at - ts|``
is itself a signal (corrupted trends and/or an aging CMOS battery).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from tests.conftest import envelope, healthy


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _hb_with_ts(device_id: str, ts_iso: str) -> dict:
    env = envelope(device_id, "heartbeat", healthy("heartbeat"))
    env["ts"] = ts_iso
    return env


def _device(client, device_id: str):
    devices = client.get("/api/v1/devices").json()
    return next((d for d in devices if d["device_id"] == device_id), None)


# --------------------------------------------------------------------------- #
# Staleness is anchored to server receipt, not the client clock (integration)
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_stale_uses_server_receipt_not_client_clock(client):
    """A box whose clock reads 2020 but just reported is NOT stale.

    Before W0.2 last_seen == client ts, so it would look silent for years.
    """
    past = _iso(datetime(2020, 1, 1, tzinfo=timezone.utc))
    r = client.post("/api/v1/ingest", json=_hb_with_ts("clock-past", past))
    assert r.status_code == 200, r.text

    d = _device(client, "clock-past")
    assert d is not None
    assert d["last_seen_age_sec"] < 600  # just received, regardless of 2020 ts
    assert d["stale"] is False


@pytest.mark.integration
def test_future_client_clock_does_not_break_staleness(client):
    """A client clock in the far future must not yield negative/garbage age."""
    future = _iso(datetime.now(timezone.utc) + timedelta(days=3650))
    client.post("/api/v1/ingest", json=_hb_with_ts("clock-future", future))

    d = _device(client, "clock-future")
    assert d["last_seen_age_sec"] >= 0
    assert d["last_seen_age_sec"] < 600
    assert d["stale"] is False


@pytest.mark.integration
def test_clock_drift_flagged_for_skewed_client(client):
    """A multi-year skew between ts and receipt surfaces a clock_drift signal."""
    past = _iso(datetime(2020, 1, 1, tzinfo=timezone.utc))
    client.post("/api/v1/ingest", json=_hb_with_ts("clock-skew", past))

    d = _device(client, "clock-skew")
    assert d["clock_drift"] is True
    assert d["clock_drift_sec"] is not None
    assert d["clock_drift_sec"] > 86400  # years late: received_at >> reported ts


@pytest.mark.integration
def test_aligned_client_clock_has_no_drift_flag(client):
    """A device reporting near server time shows no drift flag."""
    now = _iso(datetime.now(timezone.utc))
    client.post("/api/v1/ingest", json=_hb_with_ts("clock-ok", now))

    d = _device(client, "clock-ok")
    assert d["clock_drift"] is False
    assert abs(d["clock_drift_sec"]) < 600


# --------------------------------------------------------------------------- #
# Storage round-trips received_at + drift; both axes are queryable (unit)
# --------------------------------------------------------------------------- #
@pytest.fixture
def db_init(tmp_path):
    from server import db

    db.init_db(tmp_path / "t.db")
    return db


@pytest.mark.unit
def test_heartbeat_persists_received_at_and_keeps_client_ts(db_init):
    db_init.store_heartbeat(
        "d",
        "2020-01-01T00:00:00+00:00",
        {"free_space_pct": 50.0},
        received_at="2026-06-03T12:00:00+00:00",
        clock_drift_sec=12345.0,
    )
    rows = db_init.get_recent_heartbeats("d", limit=1)
    assert rows[0]["received_at"] == "2026-06-03T12:00:00+00:00"
    assert rows[0]["ts"] == "2020-01-01T00:00:00+00:00"  # client time retained
    assert rows[0]["clock_drift_sec"] == 12345.0


@pytest.mark.unit
def test_historical_series_exposes_received_at(db_init):
    db_init.store_historical(
        "d",
        "2020-01-01T00:00:00+00:00",
        {"avg_boot_ms": 21000},
        received_at="2026-06-03T12:00:00+00:00",
    )
    series = db_init.get_historical_series("d")
    assert series[0]["received_at"] == "2026-06-03T12:00:00+00:00"


@pytest.mark.unit
def test_event_window_anchored_to_received_at(db_init):
    """An ancient client event ts must not move the event out of a receipt window."""
    db_init.store_events(
        "d",
        [{"event_id": 41, "ts": "1999-01-01T00:00:00+00:00", "level": "Error"}],
        received_at="2026-06-03T12:00:00+00:00",
    )
    # windowed by server receipt: present just before receipt, absent after it
    assert db_init.count_events_since("d", [41], "2026-06-03T11:00:00+00:00") == 1
    assert db_init.count_events_since("d", [41], "2026-06-03T13:00:00+00:00") == 0
