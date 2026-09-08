"""KodSR L10: disk_key cardinality must evict INACTIVE keys, not block a
device's growth forever. A key only counts as "occupied" while it has been
seen within the activity window; a genuinely new physical disk must still be
rejected once the device already holds the cap's worth of ACTIVE keys.
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


def _disk(**kw):
    base = {"disk": "Samsung 980", "media_type": "SSD"}
    base.update(kw)
    return base


def _count(db, device_id, disk_key) -> int:
    with db._connect() as conn:
        (n,) = conn.execute(
            "SELECT COUNT(*) FROM disk_readings WHERE device_id=? AND disk_key=?",
            (device_id, disk_key),
        ).fetchone()
    return int(n)


def test_new_key_evicts_stale_inactive_keys(db_init):
    db = db_init
    old_ts = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    for i in range(32):
        db.store_disk_readings("dev-1", [_disk(serial_hash=f"key-{i}")], None, old_ts)
    now_ts = datetime.now(timezone.utc).isoformat()

    db.store_disk_readings("dev-1", [_disk(serial_hash="key-new")], None, now_ts)

    assert _count(db, "dev-1", "key-new") > 0  # the new disk's series was accepted
    assert _count(db, "dev-1", "key-0") == 0  # the stale (200d-old) keys were evicted


def _seed_row(db, device_id, disk_key, received_at) -> None:
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO disk_readings (device_id, disk_key, ts, received_at, media_type, payload)"
            " VALUES (?,?,?,?,?,?)",
            (device_id, disk_key, None, received_at, "SSD", "{}"),
        )


def test_a_device_silent_90d_does_not_wipe_its_own_reported_disk(db_init):
    """security-review CRITICAL (fix-round-1): eviction ran BEFORE the INSERT,
    so a device silent >90d that reports its OWN still-present disk saw that
    disk misclassified as "new" (its only rows are all pre-window) and wiped
    right before the fresh row landed -- net collapse of real history down to
    the single just-inserted row. Direct INSERT for seeding (not
    store_disk_readings) so these rows represent PRE-EXISTING accumulated
    history -- exactly the shape backfill_disk_readings seeds, and the shape
    the CRITICAL also wiped.
    """
    db = db_init
    old_ts = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    for _ in range(5):
        _seed_row(db, "dev-1", "A", old_ts)  # A: 5 real historical rows, now silent
    now_ts = datetime.now(timezone.utc).isoformat()

    # The device comes back online and reports its own disk A again, plus a
    # genuinely new disk C.
    db.store_disk_readings("dev-1", [_disk(serial_hash="A"), _disk(serial_hash="C")], None, now_ts)

    assert _count(db, "dev-1", "A") >= 5  # A's history was NOT wiped by its own report
    assert _count(db, "dev-1", "C") > 0  # the genuinely new disk C was stored


def test_fresh_keys_still_enforce_the_cap(db_init):
    db = db_init
    now_ts = datetime.now(timezone.utc).isoformat()
    for i in range(32):
        db.store_disk_readings("dev-1", [_disk(serial_hash=f"key-{i}")], None, now_ts)

    db.store_disk_readings("dev-1", [_disk(serial_hash="key-new")], None, now_ts)

    assert _count(db, "dev-1", "key-new") == 0  # 32 ACTIVE keys already fill the cap
