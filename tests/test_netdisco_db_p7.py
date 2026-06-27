"""Ф7 Tier-1 SNMP deepening -- additive schema migration + persistence (RED first).

The new columns (``net_links.medium``/``vlan``, ``net_devices.subtype``,
``net_interfaces.if_alias``) must be:
* present on a fresh DB (``_SCHEMA``),
* added to a pre-Ф7 DB (``_ADD_COLUMNS`` legacy ALTER), and
* persisted (COALESCE-preserve on the identity-ish fields, latest-wins on the
  structural ones), idempotently.

All table/column names are module literals; nothing here is user input.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import server.db as db


def _columns(path: Path, table: str) -> set[str]:
    con = sqlite3.connect(str(path))
    try:
        return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def _row(path: Path, sql: str, params: tuple = ()) -> dict:
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    try:
        return dict(con.execute(sql, params).fetchone())
    finally:
        con.close()


def test_fresh_db_has_the_f7_columns(tmp_path: Path) -> None:
    p = tmp_path / "srp.db"
    db.init_db(p)
    assert "medium" in _columns(p, "net_links")
    assert "vlan" in _columns(p, "net_links")
    assert "a_port" in _columns(p, "net_links")
    assert "b_port" in _columns(p, "net_links")
    assert "subtype" in _columns(p, "net_devices")
    assert "if_alias" in _columns(p, "net_interfaces")


def test_legacy_db_migrates_the_f7_columns(tmp_path: Path) -> None:
    # Simulate a pre-Ф7 DB: fresh init, then strip the Ф7 columns away and re-init.
    p = tmp_path / "srp.db"
    db.init_db(p)
    con = sqlite3.connect(str(p))
    # SQLite can't DROP COLUMN portably on old engines, so rebuild the tables
    # without the Ф7 columns to emulate a legacy schema.
    con.executescript(
        """
        CREATE TABLE legacy_links AS SELECT
          id, a_nid, b_nid, a_if, b_if, link_kind, via_source, confidence,
          first_seen, last_seen FROM net_links;
        DROP TABLE net_links;
        CREATE TABLE net_links (
          id INTEGER PRIMARY KEY AUTOINCREMENT, a_nid TEXT, b_nid TEXT,
          a_if INTEGER, b_if INTEGER, link_kind TEXT, via_source TEXT,
          confidence TEXT, first_seen TEXT, last_seen TEXT);
        INSERT INTO net_links SELECT * FROM legacy_links;
        DROP TABLE legacy_links;
        CREATE TABLE legacy_ifaces AS SELECT
          id, device_nid, if_index, name, if_type, speed_mbps, oper_up, phys_mac,
          last_seen FROM net_interfaces;
        DROP TABLE net_interfaces;
        CREATE TABLE net_interfaces (
          id INTEGER PRIMARY KEY AUTOINCREMENT, device_nid TEXT, if_index INTEGER,
          name TEXT, if_type INTEGER, speed_mbps REAL, oper_up INTEGER,
          phys_mac TEXT, last_seen TEXT);
        INSERT INTO net_interfaces SELECT * FROM legacy_ifaces;
        DROP TABLE legacy_ifaces;
        """
    )
    con.commit()
    con.close()
    assert "medium" not in _columns(p, "net_links")
    # re-init migrates the legacy DB
    db.init_db(p)
    assert "medium" in _columns(p, "net_links")
    assert "vlan" in _columns(p, "net_links")
    assert "a_port" in _columns(p, "net_links")
    assert "b_port" in _columns(p, "net_links")
    assert "subtype" in _columns(p, "net_devices")
    assert "if_alias" in _columns(p, "net_interfaces")


def test_upsert_net_link_persists_medium_and_vlan(tmp_path: Path) -> None:
    p = tmp_path / "srp.db"
    db.init_db(p)
    db.upsert_net_link(
        {
            "a_nid": "nd-mac-aa",
            "b_nid": "nd-mac-bb",
            "link_kind": "l2-edge",
            "via_source": "lldp",
            "confidence": "high",
            "medium": "wireless",
            "vlan": 42,
        }
    )
    row = _row(p, "SELECT * FROM net_links")
    assert row["medium"] == "wireless"
    assert row["vlan"] == 42


def test_upsert_net_link_coalesces_medium_latest_wins_vlan(tmp_path: Path) -> None:
    # A structural re-derivation that omits the medium keeps the known medium
    # (COALESCE); vlan is latest-wins (a structural fact per edge).
    p = tmp_path / "srp.db"
    db.init_db(p)
    base = {"a_nid": "nd-mac-aa", "b_nid": "nd-mac-bb", "link_kind": "l2-edge"}
    db.upsert_net_link(
        {**base, "via_source": "lldp", "confidence": "high", "medium": "wireless", "vlan": 42}
    )
    db.upsert_net_link({**base, "via_source": "lldp", "confidence": "high", "vlan": 7})
    row = _row(p, "SELECT * FROM net_links")
    assert row["medium"] == "wireless"  # preserved
    assert row["vlan"] == 7  # latest-wins


def test_replace_net_links_persists_medium_and_vlan(tmp_path: Path) -> None:
    p = tmp_path / "srp.db"
    db.init_db(p)
    db.replace_net_links(
        [
            {
                "a_nid": "nd-mac-aa",
                "b_nid": "nd-mac-bb",
                "link_kind": "l2-edge",
                "via_source": "lldp",
                "confidence": "high",
                "medium": "wireless",
                "vlan": 42,
            }
        ],
        {"nd-mac-aa"},
    )
    row = _row(p, "SELECT * FROM net_links")
    assert row["medium"] == "wireless"
    assert row["vlan"] == 42


def test_upsert_net_device_persists_subtype(tmp_path: Path) -> None:
    p = tmp_path / "srp.db"
    db.init_db(p)
    db.upsert_net_device({"device_nid": "nd-mac-aa", "dev_type": "endpoint", "subtype": "phone"})
    row = _row(p, "SELECT * FROM net_devices")
    assert row["subtype"] == "phone"


def test_upsert_net_device_coalesces_subtype(tmp_path: Path) -> None:
    # An inventory upsert that does not carry subtype keeps the known one.
    p = tmp_path / "srp.db"
    db.init_db(p)
    db.upsert_net_device({"device_nid": "nd-mac-aa", "dev_type": "endpoint", "subtype": "phone"})
    db.upsert_net_device({"device_nid": "nd-mac-aa", "dev_type": "endpoint"})
    row = _row(p, "SELECT * FROM net_devices")
    assert row["subtype"] == "phone"


def test_store_net_interfaces_persists_if_alias(tmp_path: Path) -> None:
    p = tmp_path / "srp.db"
    db.init_db(p)
    db.store_net_interfaces(
        "nd-mac-aa", [{"if_index": 1, "name": "eth0", "if_alias": "uplink to core"}]
    )
    row = _row(p, "SELECT * FROM net_interfaces")
    assert row["if_alias"] == "uplink to core"


def test_upsert_net_link_persists_directed_ports(tmp_path: Path) -> None:
    p = tmp_path / "srp.db"
    db.init_db(p)
    db.upsert_net_link(
        {
            "a_nid": "nd-mac-aa",
            "b_nid": "nd-mac-bb",
            "link_kind": "l2-edge",
            "via_source": "lldp",
            "confidence": "high",
            "a_port": "Gi1/0/1",
            "b_port": "Gi0/24",
        }
    )
    row = _row(p, "SELECT * FROM net_links")
    assert row["a_port"] == "Gi1/0/1"
    assert row["b_port"] == "Gi0/24"


def test_upsert_net_link_coalesces_ports(tmp_path: Path) -> None:
    # A later FDB-won re-derivation (no port labels) keeps the known LLDP labels.
    p = tmp_path / "srp.db"
    db.init_db(p)
    base = {"a_nid": "nd-mac-aa", "b_nid": "nd-mac-bb", "link_kind": "l2-edge"}
    db.upsert_net_link(
        {
            **base,
            "via_source": "lldp",
            "confidence": "high",
            "a_port": "Gi1/0/1",
            "b_port": "Gi0/24",
        }
    )
    db.upsert_net_link({**base, "via_source": "fdb_edge", "confidence": "high"})
    row = _row(p, "SELECT * FROM net_links")
    assert row["a_port"] == "Gi1/0/1"  # preserved
    assert row["b_port"] == "Gi0/24"


def test_replace_net_links_persists_directed_ports(tmp_path: Path) -> None:
    p = tmp_path / "srp.db"
    db.init_db(p)
    db.replace_net_links(
        [
            {
                "a_nid": "nd-mac-aa",
                "b_nid": "nd-mac-bb",
                "link_kind": "l2-edge",
                "via_source": "lldp",
                "confidence": "high",
                "a_port": "Gi1/0/1",
                "b_port": "Gi0/24",
            }
        ],
        {"nd-mac-aa"},
    )
    row = _row(p, "SELECT * FROM net_links")
    assert row["a_port"] == "Gi1/0/1"
    assert row["b_port"] == "Gi0/24"


def test_replace_net_links_swaps_ports_on_canonical_order(tmp_path: Path) -> None:
    # Endpoints are canonicalised a_nid <= b_nid; the ports must swap with them.
    p = tmp_path / "srp.db"
    db.init_db(p)
    db.replace_net_links(
        [
            {
                "a_nid": "nd-mac-zz",  # sorts AFTER bb -> a/b (and ports) swap
                "b_nid": "nd-mac-bb",
                "link_kind": "l2-edge",
                "via_source": "lldp",
                "confidence": "high",
                "a_port": "PortZ",
                "b_port": "PortB",
            }
        ],
        {"nd-mac-zz"},
    )
    row = _row(p, "SELECT * FROM net_links")
    assert row["a_nid"] == "nd-mac-bb"
    assert row["a_port"] == "PortB"
    assert row["b_port"] == "PortZ"
