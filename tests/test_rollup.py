"""ssd3 Ф5 (T5.1): daily percentile/count rollups of heartbeats/events into
heartbeat_rollup_daily/event_rollup_daily -- long-lookback aggregates that
outlive the raw tables' shorter retention window.

Pure SQLite; no network, no FastAPI.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.unit

# Тесты фолдят один UTC-день и читают его назад через 7-дневное окно
# get_*_rollups -- хардкод даты рано или поздно из него выпадает (тот же
# приём, что job_ts в smoke.py). "Вчера" относительно текущего момента.
_DAY = (datetime.now(timezone.utc) - timedelta(days=1)).date()


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
        _seed_heartbeat(db, "dev-1", f"{_DAY}T00:00:{i:02d}+00:00", cpu_pct=float(cpu))
    n = db.rollup_heartbeats_daily(_DAY.isoformat())
    assert n == 1
    rows = db.get_heartbeat_rollups("dev-1", 7)
    assert len(rows) == 1
    assert rows[0]["n"] == 10
    assert rows[0]["cpu_p50"] == 50.0
    assert rows[0]["cpu_p95"] == 100.0


def test_disk_ms_prefers_f4_field_falls_back_to_legacy_sec(db_init):
    db = db_init
    _seed_heartbeat(db, "dev-1", f"{_DAY}T00:00:00+00:00", disk_read_ms_p95=12.5)
    # Old (pre-Ф4) agent: no disk_read_ms_p95, only the legacy per-op seconds field.
    _seed_heartbeat(db, "dev-1", f"{_DAY}T01:00:00+00:00", disk_read_sec=0.02)
    db.rollup_heartbeats_daily(_DAY.isoformat())
    row = db.get_heartbeat_rollups("dev-1", 7)[0]
    # folded values: [12.5, 20.0 (0.02s*1000)] -> p95 of 2 points is the larger one
    assert row["disk_read_ms_p95"] == 20.0


def test_min_max_aggregates(db_init):
    db = db_init
    _seed_heartbeat(
        db,
        "dev-1",
        f"{_DAY}T00:00:00+00:00",
        mem_avail_mb=500.0,
        free_space_pct=40.0,
        handle_count_total=1000,
        uptime_hours=5.0,
    )
    _seed_heartbeat(
        db,
        "dev-1",
        f"{_DAY}T01:00:00+00:00",
        mem_avail_mb=200.0,
        free_space_pct=10.0,
        handle_count_total=2000,
        uptime_hours=10.0,
    )
    db.rollup_heartbeats_daily(_DAY.isoformat())
    row = db.get_heartbeat_rollups("dev-1", 7)[0]
    assert row["mem_avail_min"] == 200.0
    assert row["free_space_min"] == 10.0
    assert row["handles_max"] == 2000
    assert row["uptime_max"] == 10.0


def test_committed_pct_p95_and_nic_errors_max(db_init):
    db = db_init
    _seed_heartbeat(db, "dev-1", f"{_DAY}T00:00:00+00:00", committed_pct=50.0, nic_errors=2)
    _seed_heartbeat(db, "dev-1", f"{_DAY}T01:00:00+00:00", committed_pct=90.0, nic_errors=5)
    db.rollup_heartbeats_daily(_DAY.isoformat())
    row = db.get_heartbeat_rollups("dev-1", 7)[0]
    assert row["committed_pct_p95"] == 90.0  # p95 of 2 points is the larger one
    assert row["nic_errors_max"] == 5


def test_committed_pct_and_nic_errors_absent_fold_to_none_not_zero(db_init):
    """B6 6.1: a heartbeat that never carried the field must not fold to 0 --
    UNKNOWN over false confidence (a silent 0 would read as "no errors")."""
    db = db_init
    _seed_heartbeat(db, "dev-1", f"{_DAY}T00:00:00+00:00", cpu_pct=10.0)
    db.rollup_heartbeats_daily(_DAY.isoformat())
    row = db.get_heartbeat_rollups("dev-1", 7)[0]
    assert row["committed_pct_p95"] is None
    assert row["nic_errors_max"] is None


def test_legacy_db_gains_committed_pct_and_nic_errors_columns(tmp_path) -> None:
    """B6 6.1: additive migration -- a pre-B6 DB (heartbeat_rollup_daily without
    the two new columns) must open cleanly and gain them via _ADD_COLUMNS."""
    import sqlite3

    from server import db

    path = tmp_path / "legacy_rollup.db"
    conn = sqlite3.connect(path)  # старая форма таблицы -- без новых колонок
    conn.executescript(
        "CREATE TABLE heartbeat_rollup_daily ("
        " device_id TEXT NOT NULL, day TEXT NOT NULL, n INTEGER NOT NULL,"
        " cpu_p50 REAL, cpu_p95 REAL, mem_avail_min REAL, pagefile_p95 REAL,"
        " disk_read_ms_p95 REAL, disk_write_ms_p95 REAL, disk_queue_p95 REAL,"
        " handles_max INTEGER, free_space_min REAL, uptime_max REAL,"
        " PRIMARY KEY (device_id, day));"
    )
    conn.commit()
    conn.close()

    db.init_db(str(path))  # не должно бросить

    with db._connect() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(heartbeat_rollup_daily)")}
    assert {"committed_pct_p95", "nic_errors_max"} <= cols


def test_rollup_scoped_per_device(db_init):
    db = db_init
    _seed_heartbeat(db, "dev-1", f"{_DAY}T00:00:00+00:00", cpu_pct=10.0)
    _seed_heartbeat(db, "dev-2", f"{_DAY}T00:00:00+00:00", cpu_pct=90.0)
    n = db.rollup_heartbeats_daily(_DAY.isoformat())
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
    _seed_heartbeat(db, "dev-1", f"{_DAY}T00:00:00+00:00", handle_count_total=10**400)
    _seed_heartbeat(db, "dev-1", f"{_DAY}T01:00:00+00:00", handle_count_total=500)
    n = db.rollup_heartbeats_daily(_DAY.isoformat())  # must not raise
    assert n == 1
    row = db.get_heartbeat_rollups("dev-1", 7)[0]
    assert row["handles_max"] == 500  # huge value dropped, valid one survived


def test_finite_but_sqlite_overflowing_int_is_clipped_not_fatal(db_init):
    """HIGH (B6 review): _finite_float only rejects inf/nan and values that
    overflow float() itself (10**400 above). 10**20 is a perfectly finite
    float -- it survives _finite_float -- but still exceeds SQLite's INTEGER
    bind range (int64 max ~9.2e18), so a bare int(max(...)) used to raise
    `OverflowError: Python int too large to convert to SQLite INTEGER` and
    abort the whole day's rollup_heartbeats_daily call (single all-devices
    transaction), which in turn skips prune_aged for that day forever
    (_run_maintenance_sweep wraps rollup+prune in one try/except).
    """
    db = db_init
    _seed_heartbeat(
        db, "dev-1", f"{_DAY}T00:00:00+00:00", handle_count_total=10**20, nic_errors=10**20
    )
    n = db.rollup_heartbeats_daily(_DAY.isoformat())  # must not raise OverflowError
    assert n == 1
    row = db.get_heartbeat_rollups("dev-1", 7)[0]
    assert row["handles_max"] == 2**31 - 1  # clipped to the safety cap, not overflowed
    assert row["nic_errors_max"] == 2**31 - 1


def test_non_finite_float_is_skipped(db_init):
    db = db_init
    _seed_heartbeat(db, "dev-1", f"{_DAY}T00:00:00+00:00", cpu_pct=float("inf"))
    _seed_heartbeat(db, "dev-1", f"{_DAY}T01:00:00+00:00", cpu_pct=42.0)
    db.rollup_heartbeats_daily(_DAY.isoformat())
    row = db.get_heartbeat_rollups("dev-1", 7)[0]
    assert row["cpu_p95"] == 42.0  # inf excluded, not propagated into the rollup


def test_one_poisoned_device_does_not_abort_others_in_the_same_call(db_init):
    db = db_init
    _seed_heartbeat(db, "dev-bad", f"{_DAY}T00:00:00+00:00", handle_count_total=10**400)
    _seed_heartbeat(db, "dev-good", f"{_DAY}T00:00:00+00:00", cpu_pct=10.0)
    n = db.rollup_heartbeats_daily(_DAY.isoformat())
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
    _seed_event(db, "dev-1", f"{_DAY}T00:00:00+00:00", "x" * 500, 153)
    db.rollup_events_daily(_DAY.isoformat())
    rows = db.get_event_rollups("dev-1", 7)
    assert len(rows) == 1
    assert len(rows[0]["event_key"]) <= db._EVENT_SOURCE_MAX_LEN + len(":153")


def test_event_keys_per_device_day_are_capped_keeping_highest_count(db_init, monkeypatch):
    db = db_init
    monkeypatch.setattr(db, "_MAX_EVENT_KEYS_PER_DEVICE_DAY", 2)
    for i in range(5):  # 5 distinct low-count keys
        _seed_event(db, "dev-1", f"{_DAY}T00:0{i}:00+00:00", f"fake{i}", 153)
    for _ in range(3):  # one clearly-loudest key
        _seed_event(db, "dev-1", f"{_DAY}T01:00:00+00:00", "loud", 153)
    n = db.rollup_events_daily(_DAY.isoformat())
    assert n == 2  # capped from 6 distinct keys down to 2
    rows = {r["event_key"]: r["n"] for r in db.get_event_rollups("dev-1", 7)}
    assert rows.get("loud:153") == 3  # the highest-count key always survives the cap


def test_event_key_cap_is_independent_per_device(db_init, monkeypatch):
    db = db_init
    monkeypatch.setattr(db, "_MAX_EVENT_KEYS_PER_DEVICE_DAY", 1)
    _seed_event(db, "dev-1", f"{_DAY}T00:00:00+00:00", "disk", 153)
    _seed_event(db, "dev-1", f"{_DAY}T00:01:00+00:00", "Ntfs", 55)
    _seed_event(db, "dev-2", f"{_DAY}T00:00:00+00:00", "disk", 153)
    db.rollup_events_daily(_DAY.isoformat())
    assert len(db.get_event_rollups("dev-1", 7)) == 1  # capped to 1
    assert len(db.get_event_rollups("dev-2", 7)) == 1  # dev-2's own cap, unaffected


# --------------------------------------------------------------------------- #
# UTC calendar-day boundary (never the agent's clock -- received_at only)
# --------------------------------------------------------------------------- #


def test_day_boundary_is_utc_calendar_date(db_init):
    db = db_init
    day1 = _DAY - timedelta(days=1)  # two consecutive UTC days, both inside the 7d window
    day2 = _DAY
    _seed_heartbeat(db, "dev-1", f"{day1}T23:59:59+00:00", cpu_pct=1.0)
    _seed_heartbeat(db, "dev-1", f"{day2}T00:00:00+00:00", cpu_pct=2.0)
    assert db.rollup_heartbeats_daily(day1.isoformat()) == 1
    assert db.rollup_heartbeats_daily(day2.isoformat()) == 1
    rows = db.get_heartbeat_rollups("dev-1", 7)
    assert rows[0]["day"] == day2.isoformat()  # newest first
    assert rows[1]["day"] == day1.isoformat()


# --------------------------------------------------------------------------- #
# Idempotency: INSERT OR REPLACE, re-running a day never duplicates or drops
# --------------------------------------------------------------------------- #


def test_rerolling_a_day_replaces_not_duplicates(db_init):
    db = db_init
    _seed_heartbeat(db, "dev-1", f"{_DAY}T00:00:00+00:00", cpu_pct=10.0)
    db.rollup_heartbeats_daily(_DAY.isoformat())
    _seed_heartbeat(db, "dev-1", f"{_DAY}T01:00:00+00:00", cpu_pct=90.0)
    db.rollup_heartbeats_daily(_DAY.isoformat())  # re-fold the same day
    rows = db.get_heartbeat_rollups("dev-1", 7)
    assert len(rows) == 1  # PRIMARY KEY (device_id, day) -- replaced, not appended
    assert rows[0]["n"] == 2  # picked up both heartbeats on the re-fold


# --------------------------------------------------------------------------- #
# rollup_events_daily
# --------------------------------------------------------------------------- #


def test_event_rollup_counts_by_source_and_id(db_init):
    db = db_init
    _seed_event(db, "dev-1", f"{_DAY}T00:00:00+00:00", "disk", 153)
    _seed_event(db, "dev-1", f"{_DAY}T01:00:00+00:00", "disk", 153)
    _seed_event(db, "dev-1", f"{_DAY}T02:00:00+00:00", "Ntfs", 55)
    n = db.rollup_events_daily(_DAY.isoformat())
    assert n == 2  # two distinct (device, event_key) pairs
    rows = {r["event_key"]: r["n"] for r in db.get_event_rollups("dev-1", 7)}
    assert rows == {"disk:153": 2, "Ntfs:55": 1}


def test_event_rollup_scoped_per_device(db_init):
    db = db_init
    _seed_event(db, "dev-1", f"{_DAY}T00:00:00+00:00", "disk", 153)
    _seed_event(db, "dev-2", f"{_DAY}T00:00:00+00:00", "disk", 153)
    db.rollup_events_daily(_DAY.isoformat())
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
    # o5-D9 -- осознанная смена контракта: было жёсткое «вчера+сегодня», стало
    # догон от последнего свёрнутого дня с потолком _ROLLUP_CATCHUP_MAX_DAYS.
    # Главное свойство теста сохраняется: это НЕ повторный бэкфилл всей истории
    # (сырьё здесь с 2026-01-01), окно ограничено потолком догона.
    assert second["days"] <= db._ROLLUP_CATCHUP_MAX_DAYS + 1


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


def test_event_storm_is_rolled_up_before_prune(tmp_path) -> None:
    """o5-D8: обрезка по _retain_ev срабатывает В МОМЕНТ вставки, а свёртка идёт
    отдельным ночным проходом — при шторме событий всё сверх потолка исчезает
    ДО того, как его успели свернуть, и статистика теряет их навсегда."""
    from server import db

    db.init_db(str(tmp_path / "storm.db"))
    did = "storm-dev"
    n = db._retain_ev + 500
    events = [
        {
            "ts": "2026-03-01T10:00:00+00:00",
            "log": "System",
            "source": "disk",
            "event_id": 7,
            "level": "Error",
            "message": "x",
        }
        for _ in range(n)
    ]
    db.store_events(did, events, received_at="2026-03-01T10:00:00+00:00")

    rollups = db.get_event_rollups(did, 3650)
    total = sum(r["n"] for r in rollups)
    assert total == n, f"свёрнуто {total} из {n}: обрезка обогнала свёртку"


def test_rollup_catches_up_after_missed_days(tmp_path) -> None:
    """o5-D9: окно свёртки жёстко «вчера+сегодня». Сервер, простоявший выходные,
    теряет свёртку за пропущенные дни навсегда — сырьё к тому времени обрезано."""
    from datetime import date

    from server import db

    db.init_db(str(tmp_path / "catchup.db"))
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=5)

    with db._connect() as conn:  # свёртка «была» 5 дней назад -> таблица не пуста
        conn.execute(
            "INSERT INTO heartbeat_rollup_daily (device_id, day, n) VALUES (?,?,?)",
            ("catch-dev", start.isoformat(), 1),
        )
        conn.commit()

    for i in range(1, 6):  # биения в каждый пропущенный день
        day = (start + timedelta(days=i)).isoformat()
        db.store_heartbeat(
            "catch-dev",
            f"{day}T10:00:00+00:00",
            {"cpu_pct": 5.0},
            received_at=f"{day}T10:00:00+00:00",
        )

    db.run_daily_rollup()

    with db._connect() as conn:
        days = {
            r[0]
            for r in conn.execute(
                "SELECT day FROM heartbeat_rollup_daily WHERE device_id='catch-dev'"
            )
        }
    missed = {(start + timedelta(days=i)).isoformat() for i in range(1, 6)} - days
    assert not missed, f"дни не догнаны: {sorted(missed)}"
    assert isinstance(date.today(), date)


def test_rolled_up_storm_survives_later_envelope(tmp_path) -> None:
    """Ревью блока D (H1): свёртка пересчитывает счётчик из ЖИВЫХ сырых строк и
    перезаписывает результат. После обрезки сырья следующий конверт того же дня
    писал МЕНЬШЕЕ число поверх правильного — свёрнутый шторм исчезал."""
    from server import db

    db.init_db(str(tmp_path / "h1.db"))
    ev = {
        "ts": "2026-03-01T10:00:00+00:00",
        "log": "System",
        "source": "disk",
        "event_id": 7,
        "level": "Error",
        "message": "x",
    }
    n = db._retain_ev + 500
    db.store_events("h1", [dict(ev) for _ in range(n)], received_at="2026-03-01T10:00:00+00:00")
    assert sum(r["n"] for r in db.get_event_rollups("h1", 3650)) == n

    db.store_events("h1", [dict(ev)], received_at="2026-03-01T11:00:00+00:00")
    total = sum(r["n"] for r in db.get_event_rollups("h1", 3650))
    assert total >= n, f"свёртка просела до {total}: следующий конверт стёр шторм"


def test_ingest_rollup_does_not_scan_other_devices(tmp_path) -> None:
    """Ревью блока D (C1): свёртка на ingest фильтровала только по ДНЮ, то есть
    сканировала события ВСЕГО парка под глобальным локом на каждом конверте."""
    from server import db

    db.init_db(str(tmp_path / "c1.db"))
    ev = {
        "ts": "2026-03-01T10:00:00+00:00",
        "log": "System",
        "source": "disk",
        "event_id": 7,
        "level": "Error",
        "message": "x",
    }
    for i in range(5):  # чужие устройства, тот же день
        db.store_events(
            f"other{i}", [dict(ev) for _ in range(50)], received_at="2026-03-01T09:00:00+00:00"
        )

    seen: list[tuple] = []
    real_connect = db._connect

    import contextlib

    class _Rec:
        def __init__(self, c):
            self._c = c

        def execute(self, sql, params=(), *a, **kw):
            if "event_rollup_daily" in sql or "FROM events WHERE" in sql:
                seen.append((sql, params))
            return self._c.execute(sql, params, *a, **kw)

        def __getattr__(self, name):
            return getattr(self._c, name)

    @contextlib.contextmanager
    def _wrapped():
        with real_connect() as c:
            yield _Rec(c)

    db._connect = _wrapped
    try:
        db.store_events("mine", [dict(ev)], received_at="2026-03-01T09:00:00+00:00")
    finally:
        db._connect = real_connect

    reads = [(s, p) for s, p in seen if "FROM events WHERE" in s]
    assert reads, "свёртка на ingest не выполнялась"
    for sql, params in reads:
        assert "device_id" in sql, "чтение событий на ingest не ограничено устройством"
        assert "mine" in tuple(params), "фильтр по устройству не привязан к конверту"
