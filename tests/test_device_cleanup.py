"""Device-ghost cleanup: full delete + silence-based purge (2026-06-16).

The clone-safe device_id fix (`14c6a97`) makes every reinstall mint a NEW id, so
the machine's old row is orphaned and lingers forever. These tests pin the
server-side hygiene that removes such ghosts:

  * delete_device wipes a device from EVERY per-device table (no shards left);
  * a schema-introspection guard fails if a future per-device table is added but
    not registered in _DEVICE_TABLES (so the wipe can never silently miss one);
  * purge_devices_silent_for deletes only devices silent past the threshold,
    judged by the server-stamped last_seen (never the client clock).

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


def _seed_all_tables(db, device_id: str) -> None:
    """Insert exactly one row for *device_id* into every per-device table.

    Generic (PRAGMA-driven) so it does not depend on any store-function
    signature and is guaranteed to touch every table in _DEVICE_TABLES: a row
    carrying device_id, plus a dummy value for any NOT NULL column without a
    default. Autoincrement ``id`` columns are left to the engine.
    """
    with db._connect() as conn:
        for table in db._DEVICE_TABLES:
            cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
            names: list[str] = []
            values: list[object] = []
            for col in cols:
                name = col["name"]
                if name == "id":  # AUTOINCREMENT rowid alias
                    continue
                if name == "device_id":
                    names.append(name)
                    values.append(device_id)
                elif col["notnull"] and col["dflt_value"] is None:
                    names.append(name)
                    values.append(1 if "INT" in (col["type"] or "").upper() else "x")
            collist = ",".join(names)
            placeholders = ",".join("?" for _ in names)
            conn.execute(f"INSERT INTO {table} ({collist}) VALUES ({placeholders})", values)


def _count(db, table: str, device_id: str) -> int:
    with db._connect() as conn:
        return conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE device_id=?", (device_id,)
        ).fetchone()["n"]


# --------------------------------------------------------------------------- #
# _DEVICE_TABLES is complete (the safety net)
# --------------------------------------------------------------------------- #
# net_devices.device_id is a cross-domain *soft FK*: a network node (keyed by MAC)
# optionally linked to an agent, NOT an agent-owned row. On delete the link is
# cleared to NULL (the node survives) rather than row-deleted, so net_devices is
# intentionally outside _DEVICE_TABLES -- see test_delete_device_nulls_net_link.
_FK_LINK_TABLES = {"net_devices"}


def test_device_tables_constant_matches_schema(db_init):
    """Every table with a device_id column is either registered for row-deletion
    (_DEVICE_TABLES) or a documented soft-FK link table cleared to NULL."""
    db = db_init
    with db._connect() as conn:
        tables = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        with_device_col = {
            t
            for t in tables
            if any(c["name"] == "device_id" for c in conn.execute(f"PRAGMA table_info({t})"))
        }
    assert with_device_col - _FK_LINK_TABLES == set(db._DEVICE_TABLES)
    assert with_device_col >= _FK_LINK_TABLES  # the exception really carries the column


def test_delete_device_nulls_net_link(db_init):
    """delete_device clears a net_devices->agent soft FK but keeps the node: a
    purged agent leaves no dangling link, the network node itself survives."""
    db = db_init
    db.upsert_device("dev-gone", "2026-06-24T00:00:00+00:00", "1.0")
    db.upsert_net_device({"device_nid": "nd-mac-AA", "mac": "AA", "dev_type": "agent"})
    db.set_net_device_links("nd-mac-AA", device_id="dev-gone")

    assert db.delete_device("dev-gone") is True

    row = db.get_net_device("nd-mac-AA")
    assert row is not None  # the MAC-keyed network node is NOT deleted
    assert row["device_id"] is None  # the dangling agent FK is cleared


# --------------------------------------------------------------------------- #
# delete_device wipes one machine, leaves the rest
# --------------------------------------------------------------------------- #
def test_delete_device_clears_every_device_table(db_init):
    db = db_init
    _seed_all_tables(db, "dev-del")
    _seed_all_tables(db, "dev-keep")

    assert db.delete_device("dev-del") is True

    for table in db._DEVICE_TABLES:
        assert _count(db, table, "dev-del") == 0, f"{table} not cleared"
        assert _count(db, table, "dev-keep") == 1, f"{table} sibling lost"


def test_delete_device_reports_existence(db_init):
    db = db_init
    db.upsert_device("dev-x", db._now_iso(), "1.0.0")
    assert db.delete_device("dev-x") is True
    assert db.delete_device("dev-x") is False  # already gone
    assert db.delete_device("never-seen") is False


def test_reingest_after_delete_leaves_no_shards(db_init):
    """A machine that comes back after deletion starts fresh, no leftovers."""
    db = db_init
    _seed_all_tables(db, "dev-r")
    db.delete_device("dev-r")
    db.upsert_device("dev-r", db._now_iso(), "1.0.0")  # reappears (new install)

    assert _count(db, "devices", "dev-r") == 1
    for table in db._DEVICE_TABLES:
        if table == "devices":
            continue
        assert _count(db, table, "dev-r") == 0, f"{table} shard survived"


# --------------------------------------------------------------------------- #
# purge_devices_silent_for: silence judged by server-stamped last_seen
# --------------------------------------------------------------------------- #
def _iso_days_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def test_purge_deletes_silent_keeps_fresh(db_init):
    db = db_init
    old = _iso_days_ago(40)
    db.upsert_device("dev-old", old, "1.0.0", received_at=old)
    db.upsert_device("dev-fresh", db._now_iso(), "1.0.0")  # last_seen = now

    res = db.purge_devices_silent_for(30)

    assert res["deleted"] is True
    assert res["count"] == 1
    assert res["device_ids"] == ["dev-old"]
    assert _count(db, "devices", "dev-old") == 0
    assert _count(db, "devices", "dev-fresh") == 1


def test_purge_dry_run_lists_but_deletes_nothing(db_init):
    db = db_init
    old = _iso_days_ago(40)
    db.upsert_device("dev-old", old, "1.0.0", received_at=old)

    res = db.purge_devices_silent_for(30, dry_run=True)

    assert res["deleted"] is False
    assert res["device_ids"] == ["dev-old"]
    assert _count(db, "devices", "dev-old") == 1  # untouched


def test_purge_respects_threshold(db_init):
    db = db_init
    ten = _iso_days_ago(10)
    db.upsert_device("dev-10d", ten, "1.0.0", received_at=ten)

    assert db.purge_devices_silent_for(30)["count"] == 0  # 10d < 30d -> kept
    assert db.purge_devices_silent_for(7)["count"] == 1  # 10d >= 7d -> purged
    assert _count(db, "devices", "dev-10d") == 0


def test_purge_ignores_unjudgeable_last_seen(db_init):
    """A device whose last_seen cannot be parsed is never auto-deleted."""
    db = db_init
    db.upsert_device("dev-bad", db._now_iso(), "1.0.0")
    with db._connect() as conn:
        conn.execute("UPDATE devices SET last_seen=NULL WHERE device_id=?", ("dev-bad",))

    res = db.purge_devices_silent_for(30)

    assert "dev-bad" not in res["device_ids"]
    assert _count(db, "devices", "dev-bad") == 1


def test_purge_rejects_negative_days(db_init):
    with pytest.raises(ValueError):
        db_init.purge_devices_silent_for(-1)


def test_server_config_has_retention_defaults():
    from server.config import ServerConfig

    cfg = ServerConfig()
    assert cfg.device_retention_days == 30
    assert cfg.purge_interval_hours == 24
