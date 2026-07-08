"""ssd3 Ф5 (T5.1): daily percentile/count rollups of heartbeats/events into
heartbeat_rollup_daily/event_rollup_daily -- long-lookback aggregates that
outlive the raw tables' shorter retention window.

Pure SQLite; no network, no FastAPI.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def db_init(tmp_path):
    from server import db

    db.init_db(tmp_path / "t.db")
    return db


def _seed_heartbeat(db, device_id, received_at, **payload):
    with db._lock, db._connect() as conn:
        conn.execute(
            "INSERT INTO heartbeats (device_id, ts, payload, received_at) VALUES (?,?,?,?)",
            (device_id, received_at, json.dumps(payload), received_at),
        )


def _seed_event(db, device_id, received_at, source, event_id):
    with db._lock, db._connect() as conn:
        conn.execute(
            "INSERT INTO events (device_id, ts, source, event_id, level, received_at) "
            "VALUES (?,?,?,?,?,?)",
            (device_id, received_at, source, event_id, "Error", received_at),
        )


# --------------------------------------------------------------------------- #
# rollup_heartbeats_daily: percentiles on a known sample
# --------------------------------------------------------------------------- #


def test_percentiles_on_known_sample(db_init):
    db = db_init
    # cpu_pct 10,20,...,100 (10 points, nearest-rank): p50 -> 5th smallest (50), p95 -> 10th (100)
    for i, cpu in enumerate(range(10, 101, 10)):
        _seed_heartbeat(db, "dev-1", f"2026-07-08T00:00:{i:02d}+00:00", cpu_pct=float(cpu))
    n = db.rollup_heartbeats_daily("2026-07-08")
    assert n == 1
    rows = db.get_heartbeat_rollups("dev-1", 7)
    assert len(rows) == 1
    assert rows[0]["n"] == 10
    assert rows[0]["cpu_p50"] == 50.0
    assert rows[0]["cpu_p95"] == 100.0


def test_disk_ms_prefers_f4_field_falls_back_to_legacy_sec(db_init):
    db = db_init
    _seed_heartbeat(db, "dev-1", "2026-07-08T00:00:00+00:00", disk_read_ms_p95=12.5)
    # Old (pre-Ф4) agent: no disk_read_ms_p95, only the legacy per-op seconds field.
    _seed_heartbeat(db, "dev-1", "2026-07-08T01:00:00+00:00", disk_read_sec=0.02)
    db.rollup_heartbeats_daily("2026-07-08")
    row = db.get_heartbeat_rollups("dev-1", 7)[0]
    # folded values: [12.5, 20.0 (0.02s*1000)] -> p95 of 2 points is the larger one
    assert row["disk_read_ms_p95"] == 20.0


def test_min_max_aggregates(db_init):
    db = db_init
    _seed_heartbeat(
        db,
        "dev-1",
        "2026-07-08T00:00:00+00:00",
        mem_avail_mb=500.0,
        free_space_pct=40.0,
        handle_count_total=1000,
        uptime_hours=5.0,
    )
    _seed_heartbeat(
        db,
        "dev-1",
        "2026-07-08T01:00:00+00:00",
        mem_avail_mb=200.0,
        free_space_pct=10.0,
        handle_count_total=2000,
        uptime_hours=10.0,
    )
    db.rollup_heartbeats_daily("2026-07-08")
    row = db.get_heartbeat_rollups("dev-1", 7)[0]
    assert row["mem_avail_min"] == 200.0
    assert row["free_space_min"] == 10.0
    assert row["handles_max"] == 2000
    assert row["uptime_max"] == 10.0


def test_rollup_scoped_per_device(db_init):
    db = db_init
    _seed_heartbeat(db, "dev-1", "2026-07-08T00:00:00+00:00", cpu_pct=10.0)
    _seed_heartbeat(db, "dev-2", "2026-07-08T00:00:00+00:00", cpu_pct=90.0)
    n = db.rollup_heartbeats_daily("2026-07-08")
    assert n == 2
    assert db.get_heartbeat_rollups("dev-1", 7)[0]["cpu_p50"] == 10.0
    assert db.get_heartbeat_rollups("dev-2", 7)[0]["cpu_p50"] == 90.0


def test_no_rows_for_day_is_a_noop(db_init):
    db = db_init
    assert db.rollup_heartbeats_daily("2026-01-01") == 0
    assert db.get_heartbeat_rollups("dev-1", 7) == []


# --------------------------------------------------------------------------- #
# security-review: a hostile/malformed numeric value must be dropped, never
# abort the whole day's rollup (rollup_heartbeats_daily batches every device
# for one day in a single call -- one bad row must not cost every device its
# rollup, and thence the downstream age-based prune that depends on it).
# --------------------------------------------------------------------------- #


def test_oversized_int_is_skipped_not_fatal(db_init):
    db = db_init
    # An int this large raises OverflowError from float() -- HeartbeatPayload's
    # numeric fields have no upper bound in the wire contract.
    _seed_heartbeat(db, "dev-1", "2026-07-08T00:00:00+00:00", handle_count_total=10**400)
    _seed_heartbeat(db, "dev-1", "2026-07-08T01:00:00+00:00", handle_count_total=500)
    n = db.rollup_heartbeats_daily("2026-07-08")  # must not raise
    assert n == 1
    row = db.get_heartbeat_rollups("dev-1", 7)[0]
    assert row["handles_max"] == 500  # huge value dropped, valid one survived


def test_non_finite_float_is_skipped(db_init):
    db = db_init
    _seed_heartbeat(db, "dev-1", "2026-07-08T00:00:00+00:00", cpu_pct=float("inf"))
    _seed_heartbeat(db, "dev-1", "2026-07-08T01:00:00+00:00", cpu_pct=42.0)
    db.rollup_heartbeats_daily("2026-07-08")
    row = db.get_heartbeat_rollups("dev-1", 7)[0]
    assert row["cpu_p95"] == 42.0  # inf excluded, not propagated into the rollup


def test_one_poisoned_device_does_not_abort_others_in_the_same_call(db_init):
    db = db_init
    _seed_heartbeat(db, "dev-bad", "2026-07-08T00:00:00+00:00", handle_count_total=10**400)
    _seed_heartbeat(db, "dev-good", "2026-07-08T00:00:00+00:00", cpu_pct=10.0)
    n = db.rollup_heartbeats_daily("2026-07-08")
    assert n == 2  # both devices rolled up
    assert db.get_heartbeat_rollups("dev-good", 7)[0]["cpu_p50"] == 10.0


# --------------------------------------------------------------------------- #
# security-review: event_rollup_daily's PK includes event_key ("source:id"),
# and source has no max_length in the wire contract -- bound both its length
# and the number of distinct keys a device can open per day (mirrors Ф2's
# _MAX_DISK_KEYS_PER_DEVICE for disk_readings).
# --------------------------------------------------------------------------- #


def test_event_key_source_is_length_clamped(db_init):
    db = db_init
    _seed_event(db, "dev-1", "2026-07-08T00:00:00+00:00", "x" * 500, 153)
    db.rollup_events_daily("2026-07-08")
    rows = db.get_event_rollups("dev-1", 7)
    assert len(rows) == 1
    assert len(rows[0]["event_key"]) <= db._EVENT_SOURCE_MAX_LEN + len(":153")


def test_event_keys_per_device_day_are_capped_keeping_highest_count(db_init, monkeypatch):
    db = db_init
    monkeypatch.setattr(db, "_MAX_EVENT_KEYS_PER_DEVICE_DAY", 2)
    for i in range(5):  # 5 distinct low-count keys
        _seed_event(db, "dev-1", f"2026-07-08T00:0{i}:00+00:00", f"fake{i}", 153)
    for _ in range(3):  # one clearly-loudest key
        _seed_event(db, "dev-1", "2026-07-08T01:00:00+00:00", "loud", 153)
    n = db.rollup_events_daily("2026-07-08")
    assert n == 2  # capped from 6 distinct keys down to 2
    rows = {r["event_key"]: r["n"] for r in db.get_event_rollups("dev-1", 7)}
    assert rows.get("loud:153") == 3  # the highest-count key always survives the cap


def test_event_key_cap_is_independent_per_device(db_init, monkeypatch):
    db = db_init
    monkeypatch.setattr(db, "_MAX_EVENT_KEYS_PER_DEVICE_DAY", 1)
    _seed_event(db, "dev-1", "2026-07-08T00:00:00+00:00", "disk", 153)
    _seed_event(db, "dev-1", "2026-07-08T00:01:00+00:00", "Ntfs", 55)
    _seed_event(db, "dev-2", "2026-07-08T00:00:00+00:00", "disk", 153)
    db.rollup_events_daily("2026-07-08")
    assert len(db.get_event_rollups("dev-1", 7)) == 1  # capped to 1
    assert len(db.get_event_rollups("dev-2", 7)) == 1  # dev-2's own cap, unaffected


# --------------------------------------------------------------------------- #
# UTC calendar-day boundary (never the agent's clock -- received_at only)
# --------------------------------------------------------------------------- #


def test_day_boundary_is_utc_calendar_date(db_init):
    db = db_init
    _seed_heartbeat(db, "dev-1", "2026-07-08T23:59:59+00:00", cpu_pct=1.0)
    _seed_heartbeat(db, "dev-1", "2026-07-09T00:00:00+00:00", cpu_pct=2.0)
    assert db.rollup_heartbeats_daily("2026-07-08") == 1
    assert db.rollup_heartbeats_daily("2026-07-09") == 1
    rows = db.get_heartbeat_rollups("dev-1", 7)
    assert rows[0]["day"] == "2026-07-09"  # newest first
    assert rows[1]["day"] == "2026-07-08"


# --------------------------------------------------------------------------- #
# Idempotency: INSERT OR REPLACE, re-running a day never duplicates or drops
# --------------------------------------------------------------------------- #


def test_rerolling_a_day_replaces_not_duplicates(db_init):
    db = db_init
    _seed_heartbeat(db, "dev-1", "2026-07-08T00:00:00+00:00", cpu_pct=10.0)
    db.rollup_heartbeats_daily("2026-07-08")
    _seed_heartbeat(db, "dev-1", "2026-07-08T01:00:00+00:00", cpu_pct=90.0)
    db.rollup_heartbeats_daily("2026-07-08")  # re-fold the same day
    rows = db.get_heartbeat_rollups("dev-1", 7)
    assert len(rows) == 1  # PRIMARY KEY (device_id, day) -- replaced, not appended
    assert rows[0]["n"] == 2  # picked up both heartbeats on the re-fold


# --------------------------------------------------------------------------- #
# rollup_events_daily
# --------------------------------------------------------------------------- #


def test_event_rollup_counts_by_source_and_id(db_init):
    db = db_init
    _seed_event(db, "dev-1", "2026-07-08T00:00:00+00:00", "disk", 153)
    _seed_event(db, "dev-1", "2026-07-08T01:00:00+00:00", "disk", 153)
    _seed_event(db, "dev-1", "2026-07-08T02:00:00+00:00", "Ntfs", 55)
    n = db.rollup_events_daily("2026-07-08")
    assert n == 2  # two distinct (device, event_key) pairs
    rows = {r["event_key"]: r["n"] for r in db.get_event_rollups("dev-1", 7)}
    assert rows == {"disk:153": 2, "Ntfs:55": 1}


def test_event_rollup_scoped_per_device(db_init):
    db = db_init
    _seed_event(db, "dev-1", "2026-07-08T00:00:00+00:00", "disk", 153)
    _seed_event(db, "dev-2", "2026-07-08T00:00:00+00:00", "disk", 153)
    db.rollup_events_daily("2026-07-08")
    assert len(db.get_event_rollups("dev-1", 7)) == 1
    assert len(db.get_event_rollups("dev-2", 7)) == 1


def test_event_rollup_no_rows_for_day_is_a_noop(db_init):
    db = db_init
    assert db.rollup_events_daily("2026-01-01") == 0


# --------------------------------------------------------------------------- #
# get_*_rollups: days window
# --------------------------------------------------------------------------- #


def test_get_heartbeat_rollups_respects_days_window(db_init):
    import datetime as _dt

    db = db_init
    old_day = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=200)).date().isoformat()
    _seed_heartbeat(db, "dev-1", f"{old_day}T00:00:00+00:00", cpu_pct=1.0)
    db.rollup_heartbeats_daily(old_day)
    assert db.get_heartbeat_rollups("dev-1", 90) == []  # outside the 90d window
    assert len(db.get_heartbeat_rollups("dev-1", 365)) == 1


# --------------------------------------------------------------------------- #
# run_daily_rollup: first-run full backfill vs. yesterday+today thereafter
# --------------------------------------------------------------------------- #


def test_run_daily_rollup_backfills_every_raw_day_on_first_run(db_init):
    db = db_init
    _seed_heartbeat(db, "dev-1", "2026-06-01T00:00:00+00:00", cpu_pct=1.0)
    _seed_heartbeat(db, "dev-1", "2026-06-15T00:00:00+00:00", cpu_pct=2.0)
    _seed_event(db, "dev-1", "2026-06-20T00:00:00+00:00", "disk", 153)
    result = db.run_daily_rollup()
    assert result["days"] == 3  # 06-01, 06-15, 06-20 -- every distinct raw day
    days = {r["day"] for r in db.get_heartbeat_rollups("dev-1", 3650)}
    assert days == {"2026-06-01", "2026-06-15"}


def test_run_daily_rollup_second_pass_only_targets_two_days(db_init):
    db = db_init
    _seed_heartbeat(db, "dev-1", "2026-01-01T00:00:00+00:00", cpu_pct=1.0)
    _seed_heartbeat(db, "dev-1", "2026-03-01T00:00:00+00:00", cpu_pct=2.0)
    first = db.run_daily_rollup()
    assert first["days"] == 2  # empty table -> full backfill of both raw days
    second = db.run_daily_rollup()
    assert second["days"] == 2  # seeded now -> exactly yesterday+today, not a re-backfill


def test_run_daily_rollup_writes_maintenance_log(db_init):
    db = db_init
    _seed_heartbeat(db, "dev-1", "2026-07-08T00:00:00+00:00", cpu_pct=1.0)
    db.run_daily_rollup()
    with db._connect() as conn:
        row = conn.execute("SELECT action FROM maintenance_log WHERE action='rollup'").fetchone()
    assert row is not None


def test_run_daily_rollup_empty_db_is_a_noop(db_init):
    db = db_init
    result = db.run_daily_rollup()
    assert result == {"days": 0, "heartbeat_rows": 0, "event_rows": 0}
