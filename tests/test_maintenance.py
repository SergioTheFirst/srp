"""ssd3 Ф5 (T5.3): age-based prune (layered on top of the existing per-device
row caps, never replacing them), guarded VACUUM, and maintenance_log
bookkeeping.

Pure SQLite; no network, no FastAPI.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def db_init(tmp_path):
    from server import db

    db.init_db(tmp_path / "t.db")
    return db


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _seed_heartbeat(db, device_id, received_at):
    with db._lock, db._connect() as conn:
        conn.execute(
            "INSERT INTO heartbeats (device_id, ts, payload, received_at) VALUES (?,?,?,?)",
            (device_id, received_at, "{}", received_at),
        )


def _seed_event(db, device_id, received_at):
    with db._lock, db._connect() as conn:
        conn.execute(
            "INSERT INTO events (device_id, ts, source, event_id, level, received_at) "
            "VALUES (?,?,?,?,?,?)",
            (device_id, received_at, "disk", 153, "Error", received_at),
        )


# --------------------------------------------------------------------------- #
# prune_aged: age-based, layered on top of the existing count caps
# --------------------------------------------------------------------------- #


def test_prune_aged_deletes_only_rows_older_than_the_window(db_init):
    db = db_init
    _seed_heartbeat(db, "dev-1", _iso(40))  # older than 30d
    _seed_heartbeat(db, "dev-1", _iso(1))  # fresh
    deleted = db.prune_aged(heartbeat_raw_days=30, events_raw_days=90, rollup_days=730)
    assert deleted["heartbeats"] == 1
    with db._connect() as conn:
        (remaining,) = conn.execute("SELECT COUNT(*) FROM heartbeats").fetchone()
    assert remaining == 1


def test_prune_aged_events_have_an_independent_window(db_init):
    db = db_init
    _seed_event(db, "dev-1", _iso(100))  # older than the 90d events window
    _seed_event(db, "dev-1", _iso(10))
    deleted = db.prune_aged(heartbeat_raw_days=30, events_raw_days=90, rollup_days=730)
    assert deleted["events"] == 1
    with db._connect() as conn:
        (remaining,) = conn.execute("SELECT COUNT(*) FROM events").fetchone()
    assert remaining == 1


def test_prune_aged_rollups_survive_raw_prune_within_their_own_window(db_init):
    db = db_init
    old_day = (datetime.now(timezone.utc) - timedelta(days=40)).date().isoformat()
    _seed_heartbeat(db, "dev-1", f"{old_day}T00:00:00+00:00")
    db.rollup_heartbeats_daily(old_day)
    db.prune_aged(heartbeat_raw_days=30, events_raw_days=90, rollup_days=730)
    with db._connect() as conn:
        (raw_left,) = conn.execute("SELECT COUNT(*) FROM heartbeats").fetchone()
    assert raw_left == 0  # raw row is gone (older than heartbeat_raw_days)
    assert len(db.get_heartbeat_rollups("dev-1", 3650)) == 1  # rollup survives (rollup_days=730)


def test_prune_aged_drops_rollups_past_their_own_window(db_init):
    db = db_init
    old_day = (datetime.now(timezone.utc) - timedelta(days=800)).date().isoformat()
    _seed_heartbeat(db, "dev-1", f"{old_day}T00:00:00+00:00")
    db.rollup_heartbeats_daily(old_day)
    db.prune_aged(heartbeat_raw_days=30, events_raw_days=90, rollup_days=730)
    assert db.get_heartbeat_rollups("dev-1", 3650) == []


def test_prune_aged_zero_disables_that_leg(db_init):
    db = db_init
    _seed_heartbeat(db, "dev-1", _iso(400))
    db.prune_aged(heartbeat_raw_days=0, events_raw_days=90, rollup_days=730)
    with db._connect() as conn:
        (remaining,) = conn.execute("SELECT COUNT(*) FROM heartbeats").fetchone()
    assert (
        remaining == 1
    )  # heartbeat_raw_days<=0 -> that leg is off (matches device_retention_days=0)


def test_prune_aged_writes_maintenance_log_only_when_something_deleted(db_init):
    db = db_init
    db.prune_aged(heartbeat_raw_days=30, events_raw_days=90, rollup_days=730)  # nothing to delete
    with db._connect() as conn:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM maintenance_log WHERE action='prune_aged'"
        ).fetchone()
    assert n == 0
    _seed_heartbeat(db, "dev-1", _iso(400))
    db.prune_aged(heartbeat_raw_days=30, events_raw_days=90, rollup_days=730)
    with db._connect() as conn:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM maintenance_log WHERE action='prune_aged'"
        ).fetchone()
    assert n == 1


# --------------------------------------------------------------------------- #
# P0-7: hb_cutoff/ev_cutoff must match _now_iso()'s T-separated format exactly.
# SQLite compares TEXT lexicographically, and a space-separated cutoff sorts
# *after* a same-date T-separated received_at ('T' 0x54 > ' ' 0x20) regardless
# of actual time-of-day, so a row genuinely past the retention window was
# silently kept whenever it shared the cutoff's calendar date. Frozen clock
# (monkeypatch db.datetime) makes this deterministic -- no real-clock races.
# --------------------------------------------------------------------------- #


def test_prune_aged_deletes_a_row_sharing_the_cutoffs_calendar_date(db_init, monkeypatch):
    db = db_init

    class _FrozenDatetime(datetime):
        frozen = datetime(2026, 1, 5, 14, 30, 0, tzinfo=timezone.utc)

        @classmethod
        def now(cls, tz=None):
            return cls.frozen

    monkeypatch.setattr(db, "datetime", _FrozenDatetime)

    # heartbeat_raw_days=1 -> cutoff = 2026-01-04T14:30:00+00:00. This row is
    # from earlier the same calendar day as the cutoff (2026-01-04 03:00),
    # genuinely ~35.5h old -- well past the 24h window.
    same_day_old = datetime(2026, 1, 4, 3, 0, 0, tzinfo=timezone.utc).isoformat()
    _seed_heartbeat(db, "dev-1", same_day_old)

    deleted = db.prune_aged(heartbeat_raw_days=1, events_raw_days=90, rollup_days=730)
    assert deleted["heartbeats"] == 1
    with db._connect() as conn:
        (remaining,) = conn.execute("SELECT COUNT(*) FROM heartbeats").fetchone()
    assert remaining == 0


def test_prune_aged_deletes_a_row_when_the_cutoff_lands_just_after_midnight(db_init, monkeypatch):
    db = db_init

    class _FrozenDatetime(datetime):
        frozen = datetime(2026, 1, 2, 0, 30, 0, tzinfo=timezone.utc)  # 30min after midnight

        @classmethod
        def now(cls, tz=None):
            return cls.frozen

    monkeypatch.setattr(db, "datetime", _FrozenDatetime)

    # heartbeat_raw_days=1 -> cutoff = 2026-01-01T00:30:00+00:00. This row sits
    # earlier the same calendar day as the cutoff (2026-01-01 00:10), genuinely
    # ~24h20m old -- past the 24h window.
    same_day_old = datetime(2026, 1, 1, 0, 10, 0, tzinfo=timezone.utc).isoformat()
    _seed_heartbeat(db, "dev-1", same_day_old)

    deleted = db.prune_aged(heartbeat_raw_days=1, events_raw_days=90, rollup_days=730)
    assert deleted["heartbeats"] == 1
    with db._connect() as conn:
        (remaining,) = conn.execute("SELECT COUNT(*) FROM heartbeats").fetchone()
    assert remaining == 0


# --------------------------------------------------------------------------- #
# run_maintenance: PRAGMA optimize always logs; VACUUM is guarded
# --------------------------------------------------------------------------- #


def test_run_maintenance_always_logs_optimize(db_init):
    db = db_init
    result = db.run_maintenance()
    assert result["optimized"] is True
    with db._connect() as conn:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM maintenance_log WHERE action='optimize'"
        ).fetchone()
    assert n == 1


def test_run_maintenance_skips_vacuum_below_freelist_threshold(db_init, monkeypatch):
    db = db_init
    monkeypatch.setattr(db, "_VACUUM_FREELIST_RATIO", 1.1)  # unreachable ratio -> never vacuums
    result = db.run_maintenance()
    assert result["vacuumed"] is False


def test_run_maintenance_skips_vacuum_within_min_interval(db_init, monkeypatch):
    db = db_init
    monkeypatch.setattr(db, "_VACUUM_FREELIST_RATIO", -1.0)  # force "ratio exceeds threshold"
    with db._lock, db._connect() as conn:
        conn.execute(
            "INSERT INTO maintenance_log (ts, action, detail) VALUES (?,?,?)",
            (db._now_iso(), "vacuum", None),  # "just vacuumed"
        )
    result = db.run_maintenance()
    assert result["vacuumed"] is False  # too soon since the last one


def test_run_maintenance_vacuums_when_guard_conditions_are_met(db_init, monkeypatch):
    db = db_init
    monkeypatch.setattr(db, "_VACUUM_FREELIST_RATIO", -1.0)  # force "ratio exceeds threshold"
    result = db.run_maintenance()  # no prior vacuum logged -> interval guard passes too
    assert result["vacuumed"] is True
    with db._connect() as conn:
        (n,) = conn.execute("SELECT COUNT(*) FROM maintenance_log WHERE action='vacuum'").fetchone()
    assert n == 1


# --------------------------------------------------------------------------- #
# init_db wiring: retain_disk_readings is now config-driven (was a constant)
# --------------------------------------------------------------------------- #


def test_init_db_wires_retain_disk_readings(tmp_path):
    from server import db

    db.init_db(tmp_path / "t2.db", retain_disk_readings=7)
    assert db._retain_disk == 7
