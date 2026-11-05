"""W0.2 server-stamped time + clock-drift.

The server must not trust the client clock: Windows event/sample time depends on
the machine's own system clock, so staleness, trends, and event windows are
anchored to a server-stamped ``received_at``. ``ts`` is retained as the
client-reported (observed) time for compatibility. A large ``|received_at - ts|``
is itself a signal (corrupted trends and/or a drifting/misconfigured system clock).
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
# 0.3.4 #9: drift is measured at the agent's per-attempt sent_ts when present
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_replayed_envelope_drift_uses_sent_ts(client):
    """A heartbeat collected 2 h ago and replayed from the offline buffer is no clock
    problem: ts is collection time, sent_ts the moment this delivery attempt left."""
    from server import db

    now = datetime.now(timezone.utc)
    env = _hb_with_ts("clock-replay", _iso(now - timedelta(hours=2)))
    env["sent_ts"] = _iso(now)
    assert client.post("/api/v1/ingest", json=env).status_code == 200

    d = _device(client, "clock-replay")
    assert d["clock_drift"] is False
    assert abs(d["clock_drift_sec"]) < 60
    hb = db.get_recent_heartbeats("clock-replay", limit=1)[0]
    assert abs(hb["clock_drift_sec"]) < 60  # the score's clock factor reads this row
    assert hb["ts"] == env["ts"]  # the client collection time is still kept


@pytest.mark.integration
def test_old_agent_without_sent_ts_keeps_the_ts_drift(client):
    env = _hb_with_ts("clock-legacy", _iso(datetime.now(timezone.utc) - timedelta(hours=2)))
    assert client.post("/api/v1/ingest", json=env).status_code == 200

    d = _device(client, "clock-legacy")
    assert d["clock_drift"] is True
    assert d["clock_drift_sec"] == pytest.approx(7200, abs=60)


@pytest.mark.integration
def test_malformed_sent_ts_gives_no_drift(client):
    """Like a malformed ts today: drift None (no flag), never a 500."""
    env = _hb_with_ts("clock-bad-sent", _iso(datetime.now(timezone.utc) - timedelta(hours=2)))
    env["sent_ts"] = "not-a-time"
    assert client.post("/api/v1/ingest", json=env).status_code == 200

    d = _device(client, "clock-bad-sent")
    assert d["clock_drift_sec"] is None
    assert d["clock_drift"] is False


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
def test_heartbeat_payload_cannot_shadow_server_columns(db_init):
    # The payload is stored raw (extra keys allowed), so a key named like a server
    # column must not replace it: windows anchor on the server receipt (W0.2).
    forged = {
        "free_space_pct": 50.0,
        "ts": "2099-01-01T00:00:00+00:00",
        "received_at": "2099-01-01T00:00:00+00:00",
        "clock_drift_sec": 0.0,
    }
    db_init.store_heartbeat(
        "d",
        "2020-01-01T00:00:00+00:00",
        forged,
        received_at="2026-06-03T12:00:00+00:00",
        clock_drift_sec=12345.0,
    )
    row = db_init.get_recent_heartbeats("d", limit=1)[0]
    assert row["received_at"] == "2026-06-03T12:00:00+00:00"
    assert row["ts"] == "2020-01-01T00:00:00+00:00"
    assert row["clock_drift_sec"] == 12345.0
    assert row["free_space_pct"] == 50.0  # the telemetry itself is still read


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


_FORGED_SERVER_KEYS = {
    "ts": "2099-01-01T00:00:00+00:00",
    "received_at": "2099-01-01T00:00:00+00:00",
    "clock_drift_sec": 0.0,
}


@pytest.mark.unit
def test_historical_payload_cannot_shadow_server_columns(db_init):
    # Trends take their x-axis and span gates from the server receipt (W0.2,
    # calibration D1): a raw payload key must not replace a server column.
    db_init.store_historical(
        "d",
        "2020-01-01T00:00:00+00:00",
        {"avg_boot_ms": 21000, **_FORGED_SERVER_KEYS},
        received_at="2026-06-03T12:00:00+00:00",
        clock_drift_sec=12345.0,
    )
    row = db_init.get_historical_series("d")[0]
    assert row["received_at"] == "2026-06-03T12:00:00+00:00"
    assert row["ts"] == "2020-01-01T00:00:00+00:00"
    assert row["clock_drift_sec"] == 12345.0
    assert row["avg_boot_ms"] == 21000  # the telemetry itself is still read


@pytest.mark.unit
def test_disk_series_payload_cannot_shadow_server_columns(db_init):
    received = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    disk = {"serial_hash": "abc", "media_type": 4, "wear_pct": 5.0, **_FORGED_SERVER_KEYS}
    db_init.store_disk_readings("d", [disk], "2020-01-01T00:00:00+00:00", received)
    row = db_init.get_disk_series("d", "abc")[0]
    assert row["received_at"] == received
    assert row["ts"] == "2020-01-01T00:00:00+00:00"
    assert row["wear_pct"] == 5.0


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
