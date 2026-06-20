"""Phase 2: netdisco persistence layer (net_* tables + store/get).

Mirrors the printers storage pattern (COALESCE inventory + append-only readings
+ retention prune). The net_* tables are keyed by ``device_nid`` (network
identity), deliberately separate from the agent ``device_id`` lifecycle.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import server.db as db

_NET_TABLES = {
    "net_devices",
    "net_interfaces",
    "net_links",
    "net_device_readings",
    "net_topology_snapshots",
    "net_changes",
}


def _table_names(path: Path) -> set[str]:
    con = sqlite3.connect(str(path))
    try:
        return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()


def test_init_creates_all_net_tables(tmp_path: Path) -> None:
    p = tmp_path / "srp.db"
    db.init_db(p)
    assert _table_names(p) >= _NET_TABLES


def test_init_is_idempotent(tmp_path: Path) -> None:
    p = tmp_path / "srp.db"
    db.init_db(p)
    db.init_db(p)  # second init must not crash or drop data
    assert _table_names(p) >= _NET_TABLES


def test_net_tables_are_added_to_a_preexisting_db(tmp_path: Path) -> None:
    # A pre-netdisco DB has the old tables but no net_* ones; init must add them
    # (IF NOT EXISTS migration, same as the printers tables were added).
    p = tmp_path / "srp.db"
    db.init_db(p)
    con = sqlite3.connect(str(p))
    for table in _NET_TABLES:
        con.execute(f"DROP TABLE IF EXISTS {table}")
    con.commit()
    con.close()
    assert not (_NET_TABLES & _table_names(p))  # confirm the simulation removed them
    db.init_db(p)
    assert _table_names(p) >= _NET_TABLES
