"""Phase 2: netdisco persistence layer (net_* tables + store/get).

Mirrors the printers storage pattern (COALESCE inventory + append-only readings
+ retention prune). The net_* tables are keyed by ``device_nid`` (network
identity), deliberately separate from the agent ``device_id`` lifecycle.
"""

from __future__ import annotations

import json
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


# --------------------------------------------------------------------------- #
# Writers
# --------------------------------------------------------------------------- #
def _rows(path: Path, sql: str, params: tuple = ()) -> list:
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, params)]
    finally:
        con.close()


def test_upsert_net_device_coalesces_identity_and_keeps_known_type(tmp_path: Path) -> None:
    p = tmp_path / "srp.db"
    db.init_db(p)
    db.upsert_net_device(
        {
            "device_nid": "nd-mac-AA",
            "ip": "10.0.0.5",
            "vendor": "VMware",
            "dev_type": "switch",
            "status": "up",
        }
    )
    # A later transient poll missing identity must not wipe known values, and must
    # not demote a known type back to 'unknown'.
    db.upsert_net_device(
        {
            "device_nid": "nd-mac-AA",
            "ip": None,
            "vendor": None,
            "dev_type": "unknown",
            "status": "unreachable",
        }
    )
    row = _rows(p, "SELECT * FROM net_devices WHERE device_nid=?", ("nd-mac-AA",))[0]
    assert row["vendor"] == "VMware"  # COALESCE kept known vendor
    assert row["dev_type"] == "switch"  # keep-known over later 'unknown'
    assert row["status"] == "unreachable"  # status is latest-wins
    assert row["first_seen"] is not None
    assert row["last_seen"] is not None


def test_store_net_device_reading_appends_and_prunes(tmp_path: Path) -> None:
    p = tmp_path / "srp.db"
    db.init_db(p, retain_net_readings=2)
    for i in range(3):
        db.store_net_device_reading("nd-mac-AA", {"seq": i}, status="up")
    rows = _rows(
        p, "SELECT detail FROM net_device_readings WHERE device_nid=? ORDER BY id", ("nd-mac-AA",)
    )
    assert len(rows) == 2  # pruned to retain cap
    assert json.loads(rows[-1]["detail"])["seq"] == 2  # newest kept


def test_store_net_interfaces_replaces_previous(tmp_path: Path) -> None:
    p = tmp_path / "srp.db"
    db.init_db(p)
    db.store_net_interfaces(
        "nd-mac-AA",
        [
            {"if_index": 1, "name": "eth0", "oper_up": True},
            {"if_index": 2, "name": "eth1", "oper_up": False},
        ],
    )
    db.store_net_interfaces("nd-mac-AA", [{"if_index": 3, "name": "eth2", "oper_up": True}])
    rows = _rows(p, "SELECT name, oper_up FROM net_interfaces WHERE device_nid=?", ("nd-mac-AA",))
    assert [r["name"] for r in rows] == ["eth2"]  # full replace, not append
    assert rows[0]["oper_up"] == 1  # bool stored as 0/1


def test_upsert_net_link_canonicalises_and_dedups(tmp_path: Path) -> None:
    p = tmp_path / "srp.db"
    db.init_db(p)
    db.upsert_net_link(
        {
            "a_nid": "nd-z",
            "b_nid": "nd-a",
            "a_if": 9,
            "b_if": 1,
            "link_kind": "l2-edge",
            "via_source": "fdb_edge",
            "confidence": "high",
        }
    )
    # Same undirected link in reverse order -> one canonical row, latest source wins.
    db.upsert_net_link(
        {
            "a_nid": "nd-a",
            "b_nid": "nd-z",
            "link_kind": "l2-edge",
            "via_source": "lldp",
            "confidence": "high",
        }
    )
    rows = _rows(p, "SELECT a_nid, b_nid, a_if, b_if, via_source FROM net_links")
    assert len(rows) == 1
    assert rows[0]["a_nid"] == "nd-a" and rows[0]["b_nid"] == "nd-z"  # canonical a<=b
    assert rows[0]["a_if"] == 1 and rows[0]["b_if"] == 9  # ifs swapped with endpoints, COALESCEd
    assert rows[0]["via_source"] == "lldp"  # latest-wins


def test_store_topology_snapshot_appends_prunes_and_counts(tmp_path: Path) -> None:
    p = tmp_path / "srp.db"
    db.init_db(p, retain_net_snapshots=2)
    for i in range(3):
        db.store_topology_snapshot({"nodes": [{"nid": "a"}], "links": [], "seq": i})
    rows = _rows(p, "SELECT node_count, link_count FROM net_topology_snapshots ORDER BY id")
    assert len(rows) == 2
    assert rows[0]["node_count"] == 1 and rows[0]["link_count"] == 0


def test_store_net_change_appends(tmp_path: Path) -> None:
    p = tmp_path / "srp.db"
    db.init_db(p)
    db.store_net_change("appeared", device_nid="nd-mac-AA", detail={"ip": "10.0.0.5"})
    rows = _rows(p, "SELECT kind, device_nid, detail FROM net_changes")
    assert rows[0]["kind"] == "appeared"
    assert json.loads(rows[0]["detail"])["ip"] == "10.0.0.5"


# --------------------------------------------------------------------------- #
# Readers
# --------------------------------------------------------------------------- #
def test_get_net_devices_lists_and_filters_by_type(tmp_path: Path) -> None:
    p = tmp_path / "srp.db"
    db.init_db(p)
    db.upsert_net_device({"device_nid": "nd-1", "dev_type": "switch", "ip": "10.0.0.1"})
    db.upsert_net_device({"device_nid": "nd-2", "dev_type": "endpoint", "ip": "10.0.0.2"})
    assert {d["device_nid"] for d in db.get_net_devices()} == {"nd-1", "nd-2"}
    assert [d["device_nid"] for d in db.get_net_devices(dev_type="switch")] == ["nd-1"]


def test_get_net_device_includes_interfaces_and_links(tmp_path: Path) -> None:
    p = tmp_path / "srp.db"
    db.init_db(p)
    db.upsert_net_device({"device_nid": "nd-a", "ip": "10.0.0.1"})
    db.upsert_net_device({"device_nid": "nd-b", "ip": "10.0.0.2"})
    db.store_net_interfaces("nd-a", [{"if_index": 1, "name": "eth0", "oper_up": True}])
    db.upsert_net_link(
        {
            "a_nid": "nd-a",
            "b_nid": "nd-b",
            "link_kind": "l2-edge",
            "via_source": "lldp",
            "confidence": "high",
        }
    )
    dev = db.get_net_device("nd-a")
    assert dev is not None
    assert dev["device_nid"] == "nd-a"
    assert [i["name"] for i in dev["interfaces"]] == ["eth0"]
    assert len(dev["links"]) == 1 and dev["links"][0]["via_source"] == "lldp"
    assert db.get_net_device("nope") is None


def test_get_net_links_returns_all(tmp_path: Path) -> None:
    p = tmp_path / "srp.db"
    db.init_db(p)
    db.upsert_net_link(
        {
            "a_nid": "nd-a",
            "b_nid": "nd-b",
            "link_kind": "l2-edge",
            "via_source": "lldp",
            "confidence": "high",
        }
    )
    db.upsert_net_link(
        {
            "a_nid": "nd-b",
            "b_nid": "nd-c",
            "link_kind": "l2-edge",
            "via_source": "fdb_edge",
            "confidence": "medium",
        }
    )
    assert len(db.get_net_links()) == 2


def test_get_latest_topology_snapshot(tmp_path: Path) -> None:
    p = tmp_path / "srp.db"
    db.init_db(p)
    assert db.get_latest_topology_snapshot() is None  # empty DB
    db.store_topology_snapshot({"nodes": [{"nid": "a"}], "links": [], "seq": 0})
    db.store_topology_snapshot({"nodes": [{"nid": "a"}, {"nid": "b"}], "links": [], "seq": 1})
    snap = db.get_latest_topology_snapshot()
    assert snap is not None
    assert snap["node_count"] == 2  # newest snapshot
    assert snap["graph"]["seq"] == 1


def test_get_net_changes_returns_recent_parsed(tmp_path: Path) -> None:
    p = tmp_path / "srp.db"
    db.init_db(p)
    db.store_net_change("appeared", device_nid="nd-a", detail={"x": 1})
    changes = db.get_net_changes(days=30)
    assert len(changes) == 1
    assert changes[0]["kind"] == "appeared"
    assert changes[0]["detail"]["x"] == 1  # detail JSON parsed back to a dict
