"""Persistent network inventory built from the agents' existing telemetry.

Phase 3: no new probes, no agent/contract change. ``build_inventory`` consumes
the same per-agent network snapshots the live map already uses
(``db.get_network_snapshots()``) and turns them into ``NetDevice`` records:

  * each reporting agent -> a ``agent`` device, identified by its own adapter MAC
    (the identity layer: a neighbour MAC that belongs to a known agent is that
    agent, never a separate "unknown device");
  * every other ARP neighbour -> an agentless ``endpoint`` (or ``unknown`` when
    it has no MAC), vendor-hinted from the OUI seed.

Pure: ``build_inventory`` takes already-read snapshots, so it never touches the
DB or the network. ``persist_inventory`` is the thin server-bound writer.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from server import db
from server.analytics.netmap import agent_mac_index
from server.analytics.oui import normalize_mac, vendor_for_mac
from server.netdisco.identity import device_nid
from server.netdisco.models import NetDevice


def _primary_adapter(adapters: list[dict[str, Any]]) -> dict[str, Any]:
    """The adapter that identifies the agent: first one with a MAC, else first."""
    for adapter in adapters:
        if normalize_mac(adapter.get("mac")):
            return adapter
    return adapters[0] if adapters else {}


def _newer(a: Optional[str], b: Optional[str]) -> Optional[str]:
    if a is None:
        return b
    if b is None:
        return a
    return a if a >= b else b


def _blank(nid: str) -> dict[str, Any]:
    return {
        "nid": nid,
        "ip": None,
        "hostname": None,
        "mac": None,
        "vendor": None,
        "dev_type": None,
        "site_code": None,
        "status": None,
        "last_seen": None,
        "sources": set(),
    }


def _add_agents(snapshots: list[dict[str, Any]], by_nid: dict[str, dict[str, Any]]) -> None:
    for snap in snapshots:
        adapters = [a for a in (snap.get("adapters") or []) if isinstance(a, dict)]
        primary = _primary_adapter(adapters)
        mac = primary.get("mac")
        ipv4 = next((ip for ip in (primary.get("ipv4") or []) if ip), None)
        nid = device_nid(mac=mac, ip=ipv4)
        if nid == "nd-unknown":
            continue  # an agent with no usable adapter identity (rare) -> skip
        rec = by_nid.setdefault(nid, _blank(nid))
        rec["dev_type"] = "agent"
        rec["mac"] = rec["mac"] or normalize_mac(mac)
        rec["ip"] = rec["ip"] or ipv4
        rec["hostname"] = rec["hostname"] or snap.get("hostname")
        rec["vendor"] = rec["vendor"] or vendor_for_mac(mac)
        rec["site_code"] = rec["site_code"] or snap.get("site_code")
        rec["status"] = "up"  # the agent reported, so it is reachable
        rec["last_seen"] = _newer(rec["last_seen"], snap.get("last_seen"))
        rec["sources"].add("agent_self")


def _add_neighbors(
    snapshots: list[dict[str, Any]],
    by_nid: dict[str, dict[str, Any]],
    agent_macs: set[str],
) -> None:
    for snap in snapshots:
        for neighbor in snap.get("neighbors") or []:
            if not isinstance(neighbor, dict):
                continue
            mac = normalize_mac(neighbor.get("mac"))
            if mac and mac in agent_macs:
                continue  # a known agent: already its own 'agent' device
            ip = neighbor.get("ip")
            # Use the already-normalised MAC (consistent with rec["mac"] / the
            # agent-skip check); device_nid falls back to ip when it is None.
            nid = device_nid(mac=mac, ip=ip)
            if nid == "nd-unknown":
                continue
            rec = by_nid.setdefault(nid, _blank(nid))
            if rec["dev_type"] is None:
                rec["dev_type"] = "endpoint" if mac else "unknown"
            rec["mac"] = rec["mac"] or mac
            rec["ip"] = rec["ip"] or ip
            rec["vendor"] = rec["vendor"] or vendor_for_mac(neighbor.get("mac"))
            rec["last_seen"] = _newer(rec["last_seen"], snap.get("last_seen"))
            rec["sources"].add("arp")


def _to_device(rec: dict[str, Any]) -> NetDevice:
    return NetDevice(
        nid=rec["nid"],
        ip=rec["ip"],
        hostname=rec["hostname"],
        mac=rec["mac"],
        vendor=rec["vendor"],
        dev_type=rec["dev_type"] or "unknown",
        site_code=rec["site_code"],
        status=rec["status"],
        sources=tuple(sorted(rec["sources"])),
        last_seen=rec["last_seen"],
    )


def build_inventory(snapshots: list[dict[str, Any]]) -> list[NetDevice]:
    """Derive the network-device inventory from per-agent network snapshots."""
    by_nid: dict[str, dict[str, Any]] = {}
    _add_agents(snapshots, by_nid)  # agents first, so their MACs win the identity layer
    _add_neighbors(snapshots, by_nid, set(agent_mac_index(snapshots)))
    return [_to_device(by_nid[nid]) for nid in sorted(by_nid)]


def persist_inventory(
    devices: list[NetDevice],
    upsert: Callable[[dict[str, Any]], None] = db.upsert_net_device,
) -> int:
    """Write each inventory device through ``upsert`` (injectable for tests).

    Returns the count written. COALESCE in ``upsert_net_device`` means a later
    classify/probe phase enriches the same row without churn.
    """
    # last_seen is stamped server-side by upsert_net_device; sources is a
    # transient build artifact (no net_devices column).
    for device in devices:
        upsert(
            {
                "device_nid": device.nid,
                "ip": device.ip,
                "hostname": device.hostname,
                "mac": device.mac,
                "vendor": device.vendor,
                "dev_type": device.dev_type,
                "site_code": device.site_code,
                "status": device.status,
            }
        )
    return len(devices)
