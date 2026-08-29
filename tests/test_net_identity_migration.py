"""D4/KodSR M6: IP-only -> MAC identity migration inside upsert_net_device.

A host first observed only by IP (``nd-ip-<ip>``) later gets identified by MAC
(``nd-mac-<mac>``) -- e.g. a topology probe finally reads its ifTable, or
inventory correlates an ARP neighbour to a MAC. Before this fix the IP-keyed
row stayed forever and the newly-identified MAC row became a second,
permanent node for the same physical host. ``upsert_net_device`` now folds the
old row into the new identity, inside its own transaction, before it writes --
the single migration chokepoint every writer already goes through.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import server.db as db
from server.analytics.oui import normalize_mac
from server.netdisco.identity import device_nid

pytestmark = pytest.mark.unit

_MAC = "AA:BB:CC:DD:EE:FF"
_MAC_NID = "nd-mac-" + normalize_mac(_MAC)
_IP = "10.0.0.5"
_IP_NID = device_nid(ip=_IP)


@pytest.fixture
def db_init(tmp_path: Path):
    db.init_db(tmp_path / "t.db")
    return db


def test_ip_only_row_migrates_to_mac_identity_on_first_mac_sighting(db_init):
    d = db_init
    d.upsert_net_device({"device_nid": _IP_NID, "ip": _IP, "dev_type": "unknown"})
    first_seen = d.get_net_device(_IP_NID)["first_seen"]

    d.upsert_net_device({"device_nid": _MAC_NID, "mac": _MAC, "ip": _IP, "dev_type": "endpoint"})

    assert d.get_net_device(_IP_NID) is None  # old IP-only identity gone
    row = d.get_net_device(_MAC_NID)
    assert row is not None
    assert row["ip"] == _IP
    assert row["first_seen"] == first_seen  # the earlier sighting wins
    assert len(d.get_net_devices()) == 1  # not a duplicate node


def test_interfaces_and_links_are_dropped_readings_and_routes_repointed(db_init):
    """F2/LOW-1,LOW-3: net_interfaces has no unique key (a re-point would stack
    duplicates on every SNMP re-classify), and net_links must keep its
    a_nid<=b_nid canonical order (a re-point risks a (new,new) self-loop) -- both
    are simply DROPPED for the old nid instead of re-pointed; the next
    inventory/topology cycle re-derives them under the new identity.
    net_device_readings and net_routes have no such constraint and keep being
    re-pointed."""
    d = db_init
    d.upsert_net_device({"device_nid": _IP_NID, "ip": _IP, "dev_type": "unknown"})
    d.store_net_interfaces(_IP_NID, [{"if_index": 1, "name": "eth0"}])
    d.store_net_device_reading(_IP_NID, {"source": "reachability"}, status="up")
    d.add_net_route(_IP_NID, cidr="0.0.0.0/0", next_hop=_IP, ifindex=1)

    d.upsert_net_device({"device_nid": _MAC_NID, "mac": _MAC, "ip": _IP, "dev_type": "endpoint"})

    assert d.get_net_interfaces() == []  # dropped, not carried to the new identity
    assert d.get_net_device(_MAC_NID)["interfaces"] == []
    routes = d.get_net_routes(max_age_days=999)
    assert [r["device_nid"] for r in routes] == [_MAC_NID]  # readings/routes still re-pointed


def test_links_already_on_the_new_identity_survive_migration(db_init):
    """The old identity's own links are dropped (F2); a link already resolved
    directly against the new identity (e.g. by an earlier topology cycle) is
    untouched -- migration only ever touches rows keyed to the OLD nid, never
    raises on the old-nid link it drops."""
    d = db_init
    d.upsert_net_device({"device_nid": _IP_NID, "ip": _IP, "dev_type": "unknown"})
    d.upsert_net_device({"device_nid": "nd-mac-OTHER", "ip": "10.0.0.6", "dev_type": "endpoint"})
    d.upsert_net_link({"a_nid": _IP_NID, "b_nid": "nd-mac-OTHER", "link_kind": "l2"})
    d.upsert_net_device({"device_nid": _MAC_NID, "mac": _MAC, "dev_type": "endpoint"})
    d.upsert_net_link({"a_nid": _MAC_NID, "b_nid": "nd-mac-OTHER", "link_kind": "l2"})

    d.upsert_net_device({"device_nid": _MAC_NID, "mac": _MAC, "ip": _IP, "dev_type": "endpoint"})

    links = d.get_net_links()
    assert len(links) == 1  # the old-nid link is gone; the already-correct one survives
    assert {links[0]["a_nid"], links[0]["b_nid"]} == {_MAC_NID, "nd-mac-OTHER"}


def test_merge_when_both_identities_already_have_rows(db_init):
    """The MAC row is created first (e.g. by a topology probe with no IP yet),
    an unrelated IP-only sighting shows up later, then a single upsert finally
    reports both mac+ip together -- the merge branch (not the rename branch)."""
    d = db_init
    d.upsert_net_device({"device_nid": _MAC_NID, "mac": _MAC, "dev_type": "endpoint"})
    mac_first_seen = d.get_net_device(_MAC_NID)["first_seen"]
    d.upsert_net_device({"device_nid": _IP_NID, "ip": _IP, "hostname": "old-host"})
    ip_first_seen = d.get_net_device(_IP_NID)["first_seen"]
    assert mac_first_seen is not None and ip_first_seen is not None  # F10: sanity, not tautology

    d.upsert_net_device({"device_nid": _MAC_NID, "mac": _MAC, "ip": _IP, "dev_type": "endpoint"})

    assert d.get_net_device(_IP_NID) is None
    row = d.get_net_device(_MAC_NID)
    assert row["ip"] == _IP
    assert row["hostname"] == "old-host"  # filled from the merged-in old row
    assert row["first_seen"] == min(mac_first_seen, ip_first_seen)
    assert len(d.get_net_devices()) == 1


def test_merge_keeps_incoming_first_seen_when_stored_is_null(db_init):
    """F3/LOW-2: MIN(first_seen, ?) must be NULL-safe -- a stored NULL must not
    poison the MIN() and lose the incoming (old row's) value."""
    d = db_init
    d.upsert_net_device({"device_nid": _MAC_NID, "mac": _MAC, "dev_type": "endpoint"})
    with d._connect() as conn:
        conn.execute("UPDATE net_devices SET first_seen=NULL WHERE device_nid=?", (_MAC_NID,))
    d.upsert_net_device({"device_nid": _IP_NID, "ip": _IP, "hostname": "old-host"})
    ip_first_seen = d.get_net_device(_IP_NID)["first_seen"]
    assert ip_first_seen is not None

    d.upsert_net_device({"device_nid": _MAC_NID, "mac": _MAC, "ip": _IP, "dev_type": "endpoint"})

    row = d.get_net_device(_MAC_NID)
    assert row["first_seen"] == ip_first_seen  # NULL stored value didn't poison MIN()


def test_migrated_row_does_not_carry_forward_device_id_or_printer_id(db_init):
    """F1/HIGH-1 (device_id/printer_id half): these are soft FKs re-derived by
    link_identities from a MAC match on the next inventory cycle -- carrying
    them across a migration would let an IP/MAC pair inherit an unrelated
    agent/printer's FK. The surviving row always starts these NULL after a
    migration, regardless of what either row had."""
    d = db_init
    d.upsert_net_device({"device_nid": _IP_NID, "ip": _IP, "dev_type": "unknown"})
    d.set_net_device_links(_IP_NID, printer_id="prn-1")
    assert d.get_net_device(_IP_NID)["printer_id"] == "prn-1"

    d.upsert_net_device({"device_nid": _MAC_NID, "mac": _MAC, "ip": _IP, "dev_type": "endpoint"})

    assert d.get_net_device(_IP_NID) is None
    row = d.get_net_device(_MAC_NID)
    assert row is not None
    assert row["printer_id"] is None


def test_contradicting_mac_on_the_old_row_blocks_migration(db_init):
    """F1/HIGH-1: if the IP-only row already carries a DIFFERENT mac than the
    one seen in this upsert, migrating would silently fold a new physical host
    into the identity (DHCP reassignment, or -- unauthenticated ingest -- a
    hostile MAC claim for that IP). Both rows are left in place, nothing
    deleted."""
    d = db_init
    d.upsert_net_device(
        {"device_nid": _IP_NID, "ip": _IP, "mac": "11:11:11:11:11:11", "dev_type": "unknown"}
    )

    d.upsert_net_device({"device_nid": _MAC_NID, "mac": _MAC, "ip": _IP, "dev_type": "endpoint"})

    assert d.get_net_device(_IP_NID) is not None  # contradicting old row untouched
    assert d.get_net_device(_MAC_NID) is not None  # new identity also exists
    assert len(d.get_net_devices()) == 2  # nothing migrated, nothing deleted


def test_matching_mac_on_the_old_row_still_migrates(db_init):
    """A non-contradicting MAC already on the old row (same host re-observed by
    two different probes) still migrates normally."""
    d = db_init
    d.upsert_net_device({"device_nid": _IP_NID, "ip": _IP, "mac": _MAC, "dev_type": "unknown"})

    d.upsert_net_device({"device_nid": _MAC_NID, "mac": _MAC, "ip": _IP, "dev_type": "endpoint"})

    assert d.get_net_device(_IP_NID) is None
    assert d.get_net_device(_MAC_NID) is not None


def test_unrelated_rows_are_untouched(db_init):
    d = db_init
    d.upsert_net_device({"device_nid": _IP_NID, "ip": _IP, "dev_type": "unknown"})
    d.upsert_net_device(
        {"device_nid": "nd-mac-UNRELATED", "ip": "10.0.0.99", "mac": "11:22:33:44:55:66"}
    )

    d.upsert_net_device({"device_nid": _MAC_NID, "mac": _MAC, "ip": _IP, "dev_type": "endpoint"})

    assert d.get_net_device("nd-mac-UNRELATED") is not None


def test_upsert_with_no_mac_or_no_ip_does_not_migrate(db_init):
    d = db_init
    d.upsert_net_device({"device_nid": _IP_NID, "ip": _IP, "dev_type": "unknown"})

    d.upsert_net_device({"device_nid": _MAC_NID, "mac": _MAC, "dev_type": "endpoint"})  # no ip
    assert d.get_net_device(_IP_NID) is not None  # untouched -- no ip, no migration

    d.upsert_net_device({"device_nid": _MAC_NID, "ip": _IP})  # mac already known, no mac in dict
    assert d.get_net_device(_IP_NID) is not None  # still untouched
