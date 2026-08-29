"""ssd3 Ф2: disk_readings -- one append-only series PER PHYSICAL DISK, keyed by
serial_hash (or a positional fallback for pre-Ф1 payloads), so the series
survives an OS reinstall the way historical's device-envelope series does not.

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


def _disk(**kw):
    base = {"disk": "Samsung 980", "media_type": "SSD", "serial_hash": "hashabc123"}
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# store_disk_readings / get_disk_series: write/read round trip
# --------------------------------------------------------------------------- #


def test_store_and_read_round_trip(db_init):
    db = db_init
    db.store_disk_readings(
        "dev-1", [_disk(wear_pct=5.0)], "2026-07-08T00:00:00+00:00", "2026-07-08T00:00:01+00:00"
    )
    series = db.get_disk_series("dev-1", "hashabc123")
    assert len(series) == 1
    assert series[0]["wear_pct"] == 5.0
    assert series[0]["media_type"] == "SSD"
    assert series[0]["received_at"] == "2026-07-08T00:00:01+00:00"


def test_series_is_newest_first(db_init):
    db = db_init
    for i in range(3):
        db.store_disk_readings(
            "dev-1", [_disk(wear_pct=float(i))], None, f"2026-07-0{i + 1}T00:00:00+00:00"
        )
    series = db.get_disk_series("dev-1", "hashabc123")
    assert [r["wear_pct"] for r in series] == [2.0, 1.0, 0.0]


def test_series_scoped_to_device_and_disk_key(db_init):
    db = db_init
    db.store_disk_readings("dev-1", [_disk(serial_hash="A")], None, "2026-07-08T00:00:00+00:00")
    db.store_disk_readings("dev-1", [_disk(serial_hash="B")], None, "2026-07-08T00:00:00+00:00")
    db.store_disk_readings("dev-2", [_disk(serial_hash="A")], None, "2026-07-08T00:00:00+00:00")
    assert len(db.get_disk_series("dev-1", "A")) == 1
    assert len(db.get_disk_series("dev-1", "B")) == 1
    assert len(db.get_disk_series("dev-2", "A")) == 1
    assert db.get_disk_series("dev-1", "nope") == []


def test_multiple_disks_in_one_envelope_get_separate_series(db_init):
    db = db_init
    db.store_disk_readings(
        "dev-1",
        [_disk(serial_hash="A", disk="Disk A"), _disk(serial_hash="B", disk="Disk B")],
        None,
        "2026-07-08T00:00:00+00:00",
    )
    assert db.get_disk_series("dev-1", "A")[0]["disk"] == "Disk A"
    assert db.get_disk_series("dev-1", "B")[0]["disk"] == "Disk B"


def test_empty_or_non_list_storage_is_a_noop(db_init):
    db = db_init
    db.store_disk_readings("dev-1", [], None, "2026-07-08T00:00:00+00:00")
    db.store_disk_readings("", [_disk()], None, "2026-07-08T00:00:00+00:00")
    with db._connect() as conn:
        (count,) = conn.execute("SELECT COUNT(*) FROM disk_readings").fetchone()
    assert count == 0


# --------------------------------------------------------------------------- #
# Retention cap (per device+disk_key, mirrors printer_readings' pattern)
# --------------------------------------------------------------------------- #


def test_retention_caps_per_device_and_disk(db_init, monkeypatch):
    db = db_init
    monkeypatch.setattr(db, "_retain_disk", 3)
    for i in range(6):
        db.store_disk_readings(
            "dev-1", [_disk(wear_pct=float(i))], None, f"2026-07-{i + 1:02d}T00:00:00+00:00"
        )
    series = db.get_disk_series("dev-1", "hashabc123", limit=100)
    assert len(series) == 3
    assert [r["wear_pct"] for r in series] == [5.0, 4.0, 3.0]  # newest 3 survive


def test_retention_is_independent_per_disk_key(db_init, monkeypatch):
    db = db_init
    monkeypatch.setattr(db, "_retain_disk", 2)
    for i in range(5):
        db.store_disk_readings(
            "dev-1",
            [_disk(serial_hash="A", wear_pct=float(i)), _disk(serial_hash="B", wear_pct=float(i))],
            None,
            f"2026-07-{i + 1:02d}T00:00:00+00:00",
        )
    assert len(db.get_disk_series("dev-1", "A", limit=100)) == 2
    assert len(db.get_disk_series("dev-1", "B", limit=100)) == 2


# --------------------------------------------------------------------------- #
# security-review: distinct disk_key ceiling per device (retention only
# bounds rows WITHIN one key, never the number of keys a device can open)
# --------------------------------------------------------------------------- #


def test_new_disk_keys_beyond_ceiling_are_dropped(db_init, monkeypatch):
    db = db_init
    monkeypatch.setattr(db, "_MAX_DISK_KEYS_PER_DEVICE", 3)
    for i in range(5):
        db.store_disk_readings(
            "dev-1", [_disk(serial_hash=f"key-{i}")], None, "2026-07-08T00:00:00+00:00"
        )
    disks = db.list_device_disks("dev-1")
    assert len(disks) == 3  # only the ceiling's worth of distinct keys were ever created


def test_existing_disk_keys_keep_updating_past_the_ceiling(db_init, monkeypatch):
    db = db_init
    monkeypatch.setattr(db, "_MAX_DISK_KEYS_PER_DEVICE", 1)
    db.store_disk_readings(
        "dev-1", [_disk(serial_hash="A", wear_pct=1.0)], None, "2026-07-08T00:00:00+00:00"
    )
    # A new key is blocked, but the existing "A" series still accepts new readings.
    db.store_disk_readings(
        "dev-1",
        [_disk(serial_hash="A", wear_pct=2.0), _disk(serial_hash="B", wear_pct=1.0)],
        None,
        "2026-07-08T01:00:00+00:00",
    )
    assert [r["wear_pct"] for r in db.get_disk_series("dev-1", "A", limit=100)] == [2.0, 1.0]
    assert db.get_disk_series("dev-1", "B") == []


# --------------------------------------------------------------------------- #
# Fallback disk_key: deterministic when serial_hash is absent (pre-Ф1 agents)
# --------------------------------------------------------------------------- #


def test_fallback_key_used_when_no_serial_hash(db_init):
    db = db_init
    disk = {"disk": "Old SSD", "media_type": "SSD"}  # no serial_hash (pre-Ф1 shape)
    db.store_disk_readings("dev-1", [disk], None, "2026-07-08T00:00:00+00:00")
    disks = db.list_device_disks("dev-1")
    assert len(disks) == 1
    assert disks[0]["disk_key"]  # some deterministic non-empty key was assigned
    assert disks[0]["disk"] == "Old SSD"


def test_fallback_key_is_deterministic_and_position_sensitive(db_init):
    db = db_init
    a = {"disk": "X", "media_type": "SSD"}
    b = {"disk": "Y", "media_type": "SSD"}
    db.store_disk_readings("dev-1", [a], None, "2026-07-08T00:00:00+00:00")
    db.store_disk_readings("dev-1", [a], None, "2026-07-08T01:00:00+00:00")  # same disk again
    db.store_disk_readings("dev-2", [b], None, "2026-07-08T00:00:00+00:00")  # different disk
    disks_1 = db.list_device_disks("dev-1")
    disks_2 = db.list_device_disks("dev-2")
    assert len(disks_1) == 1  # the repeat write joined the SAME series, not a new one
    assert disks_1[0]["disk_key"] != disks_2[0]["disk_key"]


# --------------------------------------------------------------------------- #
# list_device_disks
# --------------------------------------------------------------------------- #


def test_list_device_disks_latest_wins_per_key(db_init):
    db = db_init
    db.store_disk_readings("dev-1", [_disk(disk="Old Name")], None, "2026-07-08T00:00:00+00:00")
    db.store_disk_readings("dev-1", [_disk(disk="New Name")], None, "2026-07-08T01:00:00+00:00")
    disks = db.list_device_disks("dev-1")
    assert len(disks) == 1
    assert disks[0]["disk"] == "New Name"


# --------------------------------------------------------------------------- #
# backfill_disk_readings: idempotent seed from historical
# --------------------------------------------------------------------------- #


def test_backfill_seeds_from_historical_when_empty(db_init):
    db = db_init
    db.store_historical(
        "dev-1",
        "2026-07-08T00:00:00+00:00",
        {"storage": [_disk(wear_pct=10.0)]},
        received_at="2026-07-08T00:00:01+00:00",
    )
    inserted = db.backfill_disk_readings()
    assert inserted == 1
    series = db.get_disk_series("dev-1", "hashabc123")
    assert len(series) == 1
    assert series[0]["wear_pct"] == 10.0


def test_backfill_replays_every_historical_row_in_order(db_init):
    db = db_init
    for i in range(3):
        db.store_historical(
            "dev-1",
            f"2026-07-0{i + 1}T00:00:00+00:00",
            {"storage": [_disk(wear_pct=float(i))]},
            received_at=f"2026-07-0{i + 1}T00:00:01+00:00",
        )
    db.backfill_disk_readings()
    series = db.get_disk_series("dev-1", "hashabc123", limit=100)
    assert [r["wear_pct"] for r in series] == [2.0, 1.0, 0.0]


def test_backfill_is_noop_when_already_populated(db_init):
    db = db_init
    db.store_historical(
        "dev-1",
        "2026-07-08T00:00:00+00:00",
        {"storage": [_disk()]},
        received_at="2026-07-08T00:00:01+00:00",
    )
    db.store_disk_readings("dev-1", [_disk()], None, "2026-07-08T00:00:00+00:00")
    inserted = db.backfill_disk_readings()  # table already non-empty
    assert inserted == 0
    assert len(db.get_disk_series("dev-1", "hashabc123")) == 1  # not duplicated


def test_backfill_skips_rows_without_storage_or_bad_json(db_init):
    db = db_init
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO historical (device_id, ts, payload, received_at) VALUES (?,?,?,?)",
            ("dev-1", "2026-07-08T00:00:00+00:00", "not json", "2026-07-08T00:00:01+00:00"),
        )
        conn.execute(
            "INSERT INTO historical (device_id, ts, payload, received_at) VALUES (?,?,?,?)",
            (
                "dev-2",
                "2026-07-08T00:00:00+00:00",
                json.dumps({"reliability_stability_index": 9.0}),  # no "storage" key
                "2026-07-08T00:00:01+00:00",
            ),
        )
    inserted = db.backfill_disk_readings()
    assert inserted == 0


def test_backfill_empty_historical_returns_zero(db_init):
    db = db_init
    assert db.backfill_disk_readings() == 0
