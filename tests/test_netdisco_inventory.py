"""Phase 3: persistent inventory built from the agents' existing ARP/adapter data.

No new probes, no agent/contract change -- build_inventory consumes the same
network snapshots the live map already uses (db.get_network_snapshots()). An
agent is identified by its own adapter MACs (a neighbour whose MAC belongs to a
known agent is that agent, never an "unknown device"); the rest are agentless
endpoints, vendor-hinted from the OUI seed.
"""

from __future__ import annotations

from typing import Any

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
