"""Phase 1 (network-map unification): the MAC identity spine.

One physical device must be one map node with one canonical card. ``net_devices``
gains additive ``device_id`` / ``printer_id`` FK columns; a single normalised-MAC
join (``link_identities``) links a network device to its agent (``devices``) and/or
printer (``printers``) record. The agent wire contract is untouched.

Pure helpers are unit-tested; the additive idempotent migration and the
link writer/reader are exercised against a throwaway SQLite file.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

# Pre-Phase-1 net_devices: every column EXCEPT device_id / printer_id. Used to
# prove the migration upgrades a real legacy DB in place without data loss.
_LEGACY_NET_DEVICES = """
CREATE TABLE net_devices (
  device_nid    TEXT PRIMARY KEY,
  ip            TEXT,
  hostname      TEXT,
  mac           TEXT,
  vendor        TEXT,
  dev_type      TEXT,
  sys_object_id TEXT,
  model         TEXT,
  serial        TEXT,
  site_code     TEXT,
  status        TEXT,
  first_seen    TEXT,
  last_seen     TEXT
)
"""


def _columns(db_file: Path, table: str) -> set:
    conn = sqlite3.connect(str(db_file))
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Pure: one shared agent-MAC index (the duplicate is removed)
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_agent_mac_index_maps_normalized_mac_to_device_id() -> None:
    from server.analytics.netmap import agent_mac_index

    snaps = [{"device_id": "dev-aaa", "adapters": [{"mac": "aa:bb:cc:dd:ee:ff"}]}]
    assert agent_mac_index(snaps) == {"AA-BB-CC-DD-EE-FF": "dev-aaa"}


# --------------------------------------------------------------------------- #
# Pure: link_identities (normalised-MAC join, IP fallback for printers)
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_link_identities_links_agent_by_mac() -> None:
    from server.netdisco.identity import link_identities

    nds = [
        {"device_nid": "nd-mac-AA-BB-CC-DD-EE-FF", "mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.1.10"}
    ]
    out = link_identities(nds, {"AA-BB-CC-DD-EE-FF": "dev-aaa"}, [])
    assert out == {"nd-mac-AA-BB-CC-DD-EE-FF": {"device_id": "dev-aaa", "printer_id": None}}


@pytest.mark.unit
def test_link_identities_links_printer_by_mac() -> None:
    from server.netdisco.identity import link_identities

    nds = [
        {"device_nid": "nd-mac-11-22-33-44-55-66", "mac": "11-22-33-44-55-66", "ip": "192.168.1.50"}
    ]
    printers = [{"printer_id": "prn-sn-XYZ", "mac": "11:22:33:44:55:66", "ip": "192.168.1.50"}]
    out = link_identities(nds, {}, printers)
    assert out == {"nd-mac-11-22-33-44-55-66": {"device_id": None, "printer_id": "prn-sn-XYZ"}}


@pytest.mark.unit
def test_link_identities_links_printer_by_ip_when_no_mac() -> None:
    from server.netdisco.identity import link_identities

    nds = [{"device_nid": "nd-ip-192.168.1.77", "mac": None, "ip": "192.168.1.77"}]
    printers = [{"printer_id": "prn-ip-192.168.1.77", "mac": None, "ip": "192.168.1.77"}]
    out = link_identities(nds, {}, printers)
    assert out == {"nd-ip-192.168.1.77": {"device_id": None, "printer_id": "prn-ip-192.168.1.77"}}


@pytest.mark.unit
def test_link_identities_no_false_link_on_mac_mismatch() -> None:
    from server.netdisco.identity import link_identities

    nds = [
        {"device_nid": "nd-mac-AA-AA-AA-AA-AA-AA", "mac": "AA-AA-AA-AA-AA-AA", "ip": "192.168.1.9"}
    ]
    out = link_identities(
        nds,
        {"BB-BB-BB-BB-BB-BB": "dev-x"},
        [{"printer_id": "p", "mac": "CC-CC-CC-CC-CC-CC", "ip": "10.0.0.1"}],
    )
    assert out == {}


@pytest.mark.unit
def test_link_identities_one_device_both_agent_and_printer() -> None:
    from server.netdisco.identity import link_identities

    mac = "DE-AD-BE-EF-00-01"
    nds = [{"device_nid": "nd-mac-" + mac, "mac": mac, "ip": "192.168.1.5"}]
    out = link_identities(
        nds, {mac: "dev-pc"}, [{"printer_id": "prn-mac-" + mac, "mac": mac, "ip": "192.168.1.5"}]
    )
    assert out == {"nd-mac-" + mac: {"device_id": "dev-pc", "printer_id": "prn-mac-" + mac}}


# --------------------------------------------------------------------------- #
# Migration: additive device_id / printer_id columns (fresh + legacy DBs)
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_init_db_creates_link_columns_on_fresh_db(tmp_path: Path) -> None:
    from server import db

    db_file = tmp_path / "fresh.db"
    db.init_db(db_file)
    cols = _columns(db_file, "net_devices")
    assert "device_id" in cols and "printer_id" in cols


@pytest.mark.integration
def test_init_db_adds_link_columns_to_legacy_net_devices(tmp_path: Path) -> None:
    from server import db

    db_file = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_file))
    conn.executescript(_LEGACY_NET_DEVICES)
    conn.execute(
        "INSERT INTO net_devices (device_nid, ip, mac, dev_type) VALUES (?,?,?,?)",
        ("nd-old", "192.168.1.5", "AA-BB-CC-DD-EE-FF", "agent"),
    )
    conn.commit()
    conn.close()

    db.init_db(db_file)  # must add the columns idempotently, no backfill KeyError

    cols = _columns(db_file, "net_devices")
    assert "device_id" in cols and "printer_id" in cols
    row = db.get_net_device("nd-old")
    assert row is not None and row["ip"] == "192.168.1.5"  # legacy data preserved


# --------------------------------------------------------------------------- #
# DB: set / get link writer + reader (COALESCE-preserve, no flapping)
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_set_and_get_net_device_links_roundtrip(tmp_path: Path) -> None:
    from server import db

    db.init_db(tmp_path / "x.db")
    db.upsert_net_device({"device_nid": "nd-mac-AA", "mac": "AA", "dev_type": "agent"})
    db.set_net_device_links("nd-mac-AA", device_id="dev-1", printer_id="prn-1")
    row = db.get_net_device("nd-mac-AA")
    assert row is not None
    assert row["device_id"] == "dev-1"
    assert row["printer_id"] == "prn-1"


@pytest.mark.integration
def test_set_net_device_links_none_preserves_existing(tmp_path: Path) -> None:
    from server import db

    db.init_db(tmp_path / "x.db")
    db.upsert_net_device({"device_nid": "nd-mac-AA", "mac": "AA"})
    db.set_net_device_links("nd-mac-AA", "dev-1", "prn-1")
    db.set_net_device_links("nd-mac-AA", None, None)  # a transient miss must not wipe a known link
    row = db.get_net_device("nd-mac-AA")
    assert row is not None
    assert row["device_id"] == "dev-1"
    assert row["printer_id"] == "prn-1"
