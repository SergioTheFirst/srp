"""W0.1 append-only longitudinal storage (historical + scores).

These tests pin the P0 foundation that everything analytical depends on:
historical readings and computed scores must accumulate as a time series
(not overwrite latest-wins), so trends ("is it getting worse?") and future
label loops are possible. Pure SQLite; no network, no FastAPI.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def db_init(tmp_path):
    from server import db

    db.init_db(tmp_path / "t.db")
    return db


# --------------------------------------------------------------------------- #
# historical is append-only
# --------------------------------------------------------------------------- #
def test_historical_appends_not_overwrites(db_init):
    """Two stores for one device keep both rows, not the latest only."""
    db_init.store_historical("dev-1", "2026-01-01T00:00:00Z", {"avg_boot_ms": 21000})
    db_init.store_historical("dev-1", "2026-01-02T00:00:00Z", {"avg_boot_ms": 23000})

    series = db_init.get_historical_series("dev-1")
    assert len(series) == 2


def test_get_historical_returns_latest(db_init):
    """get_historical still returns the most recent reading (newest by id)."""
    db_init.store_historical("dev-1", "2026-01-01T00:00:00Z", {"avg_boot_ms": 21000})
    db_init.store_historical("dev-1", "2026-01-02T00:00:00Z", {"avg_boot_ms": 23000})

    latest = db_init.get_historical("dev-1")
    assert latest is not None
    assert latest["avg_boot_ms"] == 23000


def test_historical_series_newest_first(db_init):
    """The series is ordered newest-first for trend rendering."""
    db_init.store_historical("dev-1", "2026-01-01T00:00:00Z", {"avg_boot_ms": 21000})
    db_init.store_historical("dev-1", "2026-01-02T00:00:00Z", {"avg_boot_ms": 23000})

    series = db_init.get_historical_series("dev-1")
    assert series[0]["avg_boot_ms"] == 23000
    assert series[1]["avg_boot_ms"] == 21000


def test_historical_series_respects_limit(db_init):
    """A limit caps the number of rows returned (most recent kept)."""
    for i in range(5):
        db_init.store_historical("dev-1", f"2026-01-0{i + 1}T00:00:00Z", {"avg_boot_ms": 1000 * i})

    series = db_init.get_historical_series("dev-1", limit=2)
    assert len(series) == 2
    assert series[0]["avg_boot_ms"] == 4000


def test_historical_retention_caps_rows(tmp_path):
    """Beyond the retention cap, oldest historical rows are pruned per device."""
    from server import db

    db.init_db(tmp_path / "t.db", retain_historical=3)
    for i in range(6):
        db.store_historical("dev-1", f"2026-01-0{i + 1}T00:00:00Z", {"avg_boot_ms": i})

    series = db.get_historical_series("dev-1", limit=100)
    assert len(series) == 3
    assert series[0]["avg_boot_ms"] == 5  # newest survives
    assert series[-1]["avg_boot_ms"] == 3  # rows 0,1,2 pruned


# --------------------------------------------------------------------------- #
# scores are append-only
# --------------------------------------------------------------------------- #
def test_scores_append_not_overwrite(db_init):
    """Two store_scores for one device keep both rows (score history)."""
    db_init.store_scores("dev-1", "2026-01-01T00:00:00Z", {"performance": 90.0, "risk": {}})
    db_init.store_scores("dev-1", "2026-01-02T00:00:00Z", {"performance": 70.0, "risk": {}})

    series = db_init.get_score_series("dev-1")
    assert len(series) == 2
    assert series[0]["performance"] == 70.0  # newest first


def test_get_device_returns_latest_scores(seeded_client_db):
    """get_device exposes the most recent scores after multiple recomputes."""
    db = seeded_client_db
    db.store_scores("dev-x", "2026-01-01T00:00:00Z", {"performance": 50.0, "risk": {}})
    db.store_scores("dev-x", "2026-01-02T00:00:00Z", {"performance": 88.0, "risk": {}})
    db.upsert_device("dev-x", "2026-01-02T00:00:00Z", "0.1.0")

    dev = db.get_device("dev-x")
    assert dev is not None
    assert dev["scores"]["performance"] == 88.0


# --------------------------------------------------------------------------- #
# get_devices must stay one-row-per-device despite append-only joins
# --------------------------------------------------------------------------- #
def test_get_devices_no_duplication_with_history(db_init):
    """Multiple historical + score rows must not multiply device list rows."""
    db_init.upsert_device("dev-1", "2026-01-01T00:00:00Z", "0.1.0", hostname="H1")
    for i in range(3):
        db_init.store_historical("dev-1", f"2026-01-0{i + 1}T00:00:00Z", {"avg_boot_ms": i})
        db_init.store_scores(
            "dev-1", f"2026-01-0{i + 1}T00:00:00Z", {"performance": 90.0 - i, "risk": {}}
        )

    devices = db_init.get_devices()
    assert len([d for d in devices if d["device_id"] == "dev-1"]) == 1
    only = next(d for d in devices if d["device_id"] == "dev-1")
    assert only["performance"] == 88.0  # latest score, not an older one


# --------------------------------------------------------------------------- #
# legacy DB migration: old PK(device_id) latest-wins -> append-only
# --------------------------------------------------------------------------- #
def test_migrates_legacy_latest_wins_db(tmp_path):
    """A pre-W0.1 DB (PRIMARY KEY device_id, no id col) migrates losslessly."""
    from server import db

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE historical (device_id TEXT PRIMARY KEY, ts TEXT, payload TEXT)")
    conn.execute(
        "INSERT INTO historical VALUES (?,?,?)",
        ("dev-1", "2026-01-01T00:00:00Z", json.dumps({"avg_boot_ms": 21000})),
    )
    conn.execute(
        """CREATE TABLE scores (
             device_id TEXT PRIMARY KEY, ts TEXT, performance REAL, reliability REAL,
             wear REAL, risk_exposure REAL, risk TEXT)"""
    )
    conn.execute(
        "INSERT INTO scores VALUES (?,?,?,?,?,?,?)",
        ("dev-1", "2026-01-01T00:00:00Z", 95.0, 99.0, 100.0, 5.0, json.dumps({})),
    )
    conn.commit()
    conn.close()

    db.init_db(path)  # must migrate in place without losing the existing row

    latest_hist = db.get_historical("dev-1")
    assert latest_hist is not None and latest_hist["avg_boot_ms"] == 21000

    # new shape now supports append: a second write keeps both
    db.store_historical("dev-1", "2026-01-02T00:00:00Z", {"avg_boot_ms": 22000})
    assert len(db.get_historical_series("dev-1")) == 2

    scores = db.get_score_series("dev-1")
    assert len(scores) == 1 and scores[0]["performance"] == 95.0


def test_init_db_twice_is_noop(tmp_path):
    """A second init_db on an already-migrated DB preserves data (idempotent
    startup) and must not raise."""
    from server import db

    path = tmp_path / "t.db"
    db.init_db(path)
    db.store_scores("dev-1", "2026-01-01T00:00:00Z", {"performance": 60.0, "risk": {}})

    db.init_db(path)  # second startup: no migration, no wipe

    series = db.get_score_series("dev-1")
    assert len(series) == 1 and series[0]["performance"] == 60.0


def test_migration_recovers_from_orphan_shadow(tmp_path):
    """A crashed earlier migration can leave a `__new` shadow table. Re-running
    init_db must DROP it and migrate the legacy table cleanly without losing the
    existing row (pins the necessity of DROP TABLE IF EXISTS ..__new)."""
    from server import db

    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE historical (device_id TEXT PRIMARY KEY, ts TEXT, payload TEXT)")
    conn.execute(
        "INSERT INTO historical VALUES (?,?,?)",
        ("dev-1", "2026-01-01T00:00:00Z", json.dumps({"avg_boot_ms": 7000})),
    )
    conn.execute(
        """CREATE TABLE historical__new (
             id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT, ts TEXT, payload TEXT)"""
    )
    conn.commit()
    conn.close()

    db.init_db(path)  # DROP orphan shadow -> migrate legacy -> ok

    latest = db.get_historical("dev-1")
    assert latest is not None and latest["avg_boot_ms"] == 7000
    db.store_historical("dev-1", "2026-01-02T00:00:00Z", {"avg_boot_ms": 8000})
    assert len(db.get_historical_series("dev-1")) == 2


def test_connect_closes_connection_after_with_block(db_init):
    """`with _connect() as conn:` must close conn on exit, not just commit
    (P3-1) -- else the file descriptor leaks across ~120 call sites."""
    with db_init._connect() as conn:
        conn.execute("SELECT 1")

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        conn.execute("SELECT 1")


@pytest.fixture
def seeded_client_db(tmp_path):
    from server import db

    db.init_db(tmp_path / "t.db")
    return db
