"""Phase 3: persistent inventory built from the agents' existing ARP/adapter data.

No new probes, no agent/contract change -- build_inventory consumes the same
network snapshots the live map already uses (db.get_network_snapshots()). An
agent is identified by its own adapter MACs (a neighbour whose MAC belongs to a
known agent is that agent, never an "unknown device"); the rest are agentless
endpoints, vendor-hinted from the OUI seed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import server.db as db
from server.netdisco.inventory import build_inventory, persist_inventory

# Two agents on one subnet. Each sees the gateway (VMware OUI 00-50-56), the
# other agent, and (agent A only) a VirtualBox host (OUI 08-00-27).
_SNAP_A: dict[str, Any] = {
    "device_id": "dev-A",
    "hostname": "PC-A",
    "site_code": "HQ",
    "last_seen": "2026-06-20T10:00:00+00:00",
    "adapters": [
        {
            "mac": "AA-BB-CC-DD-EE-01",
            "ipv4": ["10.0.0.10"],
            "kind": "ethernet",
            "up": True,
            "gateway": "10.0.0.1",
        }
    ],
    "neighbors": [
        {"ip": "10.0.0.1", "mac": "00-50-56-AA-BB-CC", "state": "reachable"},
        {"ip": "10.0.0.20", "mac": "AA-BB-CC-DD-EE-02", "state": "stale"},  # = agent B
        {"ip": "10.0.0.30", "mac": "08-00-27-11-22-33", "state": "reachable"},
    ],
    "quality": [],
}
_SNAP_B: dict[str, Any] = {
    "device_id": "dev-B",
    "hostname": "PC-B",
    "site_code": "HQ",
    "last_seen": "2026-06-20T10:01:00+00:00",
    "adapters": [
        {
            "mac": "AA-BB-CC-DD-EE-02",
            "ipv4": ["10.0.0.20"],
            "kind": "ethernet",
            "up": True,
            "gateway": "10.0.0.1",
        }
    ],
    "neighbors": [
        {"ip": "10.0.0.1", "mac": "00-50-56-AA-BB-CC", "state": "reachable"},
        {"ip": "10.0.0.10", "mac": "AA-BB-CC-DD-EE-01", "state": "reachable"},  # = agent A
    ],
}


def test_agents_are_classified_as_agent_with_their_identity() -> None:
    by = {d.nid: d for d in build_inventory([_SNAP_A, _SNAP_B])}
    assert by["nd-mac-AA-BB-CC-DD-EE-01"].dev_type == "agent"
    assert by["nd-mac-AA-BB-CC-DD-EE-01"].hostname == "PC-A"
    assert by["nd-mac-AA-BB-CC-DD-EE-01"].ip == "10.0.0.10"
    assert by["nd-mac-AA-BB-CC-DD-EE-02"].dev_type == "agent"


def test_agentless_neighbor_becomes_endpoint_with_oui_vendor() -> None:
    by = {d.nid: d for d in build_inventory([_SNAP_A, _SNAP_B])}
    gw = by["nd-mac-00-50-56-AA-BB-CC"]
    assert gw.dev_type == "endpoint"
    assert gw.vendor == "VMware"  # OUI seed 00-50-56
    assert "arp" in gw.sources


def test_known_agent_macs_are_not_duplicated_as_endpoints() -> None:
    inv = build_inventory([_SNAP_A, _SNAP_B])
    # Each agent's MAC appears in the other's neighbour list but must stay one
    # 'agent' device, never a second 'endpoint'.
    assert sum(1 for d in inv if d.dev_type == "agent") == 2
    assert all(d.nid != "nd-mac-AA-BB-CC-DD-EE-01" or d.dev_type == "agent" for d in inv)


def test_neighbor_seen_by_multiple_agents_is_deduped() -> None:
    inv = build_inventory([_SNAP_A, _SNAP_B])
    gws = [d for d in inv if d.nid == "nd-mac-00-50-56-AA-BB-CC"]
    assert len(gws) == 1  # the gateway is seen by both agents -> one device


def test_oui_vendor_resolved_for_known_prefix() -> None:
    inv = build_inventory([_SNAP_A, _SNAP_B])
    vbox = next(d for d in inv if d.nid == "nd-mac-08-00-27-11-22-33")
    assert vbox.vendor == "VirtualBox"
    assert vbox.dev_type == "endpoint"


def test_empty_snapshots_yield_empty_inventory() -> None:
    assert build_inventory([]) == []


def test_persist_inventory_writes_each_device_through_upsert() -> None:
    captured: list[dict[str, Any]] = []
    devices = build_inventory([_SNAP_A, _SNAP_B])
    written = persist_inventory(devices, upsert=captured.append)
    assert written == len(devices)
    agent_row = next(c for c in captured if c["device_nid"] == "nd-mac-AA-BB-CC-DD-EE-01")
    assert agent_row["dev_type"] == "agent"
    assert agent_row["hostname"] == "PC-A"


# --------------------------------------------------------------------------- #
# T2: agent-resolved NetBIOS neighbor name seeds an inventory hostname hint    #
# --------------------------------------------------------------------------- #

_SNAP_NETBIOS: dict[str, Any] = {
    "device_id": "dev-D",
    "hostname": "PC-D",
    "site_code": "HQ",
    "last_seen": "2026-07-01T00:00:00+00:00",
    "adapters": [{"mac": "AA-BB-CC-DD-EE-04", "ipv4": ["10.0.0.40"]}],
    "neighbors": [
        {"ip": "10.0.0.7", "mac": "22-33-44-55-66-77", "name": "SKPD3", "name_source": "netbios"}
    ],
}


def test_neighbor_netbios_name_seeds_hostname_hint() -> None:
    devices = build_inventory([_SNAP_NETBIOS])
    neighbor = next(d for d in devices if d.nid == "nd-mac-22-33-44-55-66-77")
    assert neighbor.hostname_hint == "SKPD3"
    assert neighbor.hostname is None  # the plain upsert field is untouched


def test_neighbor_without_name_has_no_hostname_hint() -> None:
    devices = build_inventory([_SNAP_A, _SNAP_B])
    gw = next(d for d in devices if d.nid == "nd-mac-00-50-56-AA-BB-CC")
    assert gw.hostname_hint is None


def test_hostile_netbios_name_is_dropped_at_the_boundary() -> None:
    # A hostile/MITM agent bypasses the client-side _clean_name filter and the
    # RAW payload persists, so the name is allowlist-cleaned here: control /
    # markup / whitespace / over-length bytes never become a device hostname.
    def _snap(name: str) -> dict:
        return {
            "device_id": "dev-x",
            "last_seen": "2026-07-01T00:00:00+00:00",
            "adapters": [{"mac": "AA-BB-CC-DD-EE-09", "ipv4": ["10.0.0.90"]}],
            "neighbors": [{"ip": "10.0.0.7", "mac": "22-33-44-55-66-77", "name": name}],
        }

    for hostile in ("<img src=x onerror=alert(1)>", "a b c", '"><script>', "x" * 40, "\x00evil"):
        devices = build_inventory([_snap(hostile)])
        neighbor = next(d for d in devices if d.nid == "nd-mac-22-33-44-55-66-77")
        assert neighbor.hostname_hint is None, hostile

    devices = build_inventory([_snap("SKPD3")])  # a clean name still passes
    neighbor = next(d for d in devices if d.nid == "nd-mac-22-33-44-55-66-77")
    assert neighbor.hostname_hint == "SKPD3"


def test_persist_inventory_fills_hostname_from_netbios_hint_when_empty() -> None:
    devices = build_inventory([_SNAP_NETBIOS])
    filled: dict[str, dict[str, Any]] = {}
    persist_inventory(
        devices, upsert=lambda d: None, fill=lambda nid, **kw: filled.__setitem__(nid, kw)
    )
    assert filled["nd-mac-22-33-44-55-66-77"]["hostname"] == "SKPD3"


def test_persist_inventory_does_not_fill_when_no_netbios_hint() -> None:
    filled: dict[str, dict[str, Any]] = {}
    persist_inventory(
        build_inventory([_SNAP_A, _SNAP_B]),
        upsert=lambda d: None,
        fill=lambda nid, **kw: filled.__setitem__(nid, kw),
    )
    assert filled == {}  # none of _SNAP_A/_SNAP_B's neighbors carry a name


def test_persist_inventory_netbios_hint_never_overrides_a_stronger_hostname(
    tmp_path: Path,
) -> None:
    """A NetBIOS-derived hint must only ever fill an EMPTY hostname -- an
    existing SNMP-validated name (or any richer prior value) always wins.
    Exercises the real fill_net_device_identity COALESCE, not a fake."""
    p = tmp_path / "srp.db"
    db.init_db(p)
    nid = "nd-mac-22-33-44-55-66-77"
    db.upsert_net_device({"device_nid": nid, "hostname": "switch-core.local"})

    devices = build_inventory([_SNAP_NETBIOS])
    persist_inventory(devices, upsert=db.upsert_net_device, fill=db.fill_net_device_identity)

    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    try:
        row = dict(con.execute("SELECT * FROM net_devices WHERE device_nid=?", (nid,)).fetchone())
    finally:
        con.close()
    assert row["hostname"] == "switch-core.local"  # SNMP-validated name wins


def test_persist_inventory_netbios_hint_fills_a_real_empty_row(tmp_path: Path) -> None:
    """The positive counterpart: a genuinely empty hostname IS filled end to end."""
    p = tmp_path / "srp.db"
    db.init_db(p)
    devices = build_inventory([_SNAP_NETBIOS])
    persist_inventory(devices, upsert=db.upsert_net_device, fill=db.fill_net_device_identity)

    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    try:
        row = dict(
            con.execute(
                "SELECT * FROM net_devices WHERE device_nid=?",
                ("nd-mac-22-33-44-55-66-77",),
            ).fetchone()
        )
    finally:
        con.close()
    assert row["hostname"] == "SKPD3"
