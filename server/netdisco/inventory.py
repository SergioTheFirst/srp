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
from server.netdisco import passive
from server.netdisco.identity import device_nid
from server.netdisco.models import NetDevice
from server.printers.discovery import is_rfc1918, is_rfc1918_cidr


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
        "hostname_hint": None,
        "mac": None,
        "vendor": None,
        "dev_type": None,
        "site_code": None,
        "status": None,
        "last_seen": None,
        "sources": set(),
    }


_MAX_HINT_NAME = 15  # a real NetBIOS name is <=15 chars


def _clean_hint(name: Any) -> Optional[str]:
    """A neighbour NetBIOS name safe to store as a hostname, else ``None``.

    ``NetNeighbor.name`` is only length-capped by the contract and the RAW agent
    payload is what persists (a schema validator would only reject the whole
    envelope), so a hostile/MITM agent can smuggle arbitrary bytes here. Mirror
    the agent's own ``lan_names._clean_name`` allowlist at this consumption
    boundary (defense-in-depth): control/markup/whitespace bytes are dropped
    rather than becoming a device hostname. ``isalnum`` keeps legit non-ASCII
    hostnames while rejecting ``< > " / :`` and spaces."""
    if not isinstance(name, str):
        return None
    text = name.strip()
    if not text or len(text) > _MAX_HINT_NAME:
        return None
    if not all(c.isalnum() or c in "-._" for c in text):
        return None
    return text


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
            # T2: agent-resolved NetBIOS name, a lowest-priority hint only --
            # never assigned to rec["hostname"] itself, which upsert_net_device
            # would let unconditionally overwrite a stronger (e.g. SNMP) name.
            # Allowlist-cleaned at this trust boundary (raw payload persists).
            rec["hostname_hint"] = rec["hostname_hint"] or _clean_hint(neighbor.get("name"))
            rec["last_seen"] = _newer(rec["last_seen"], snap.get("last_seen"))
            rec["sources"].add("arp")


def _to_device(rec: dict[str, Any]) -> NetDevice:
    return NetDevice(
        nid=rec["nid"],
        ip=rec["ip"],
        hostname=rec["hostname"],
        hostname_hint=rec["hostname_hint"],
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
    fill: Callable[..., None] = db.fill_net_device_identity,
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
        # hostname_hint (T2: NetBIOS-resolved neighbor name) is a LOW-priority
        # hint -- routed through fill's existing-wins COALESCE (not the upsert
        # above, whose COALESCE lets a fresh value win) so it only ever fills
        # an empty hostname and can never overwrite a validated SNMP name.
        if device.hostname_hint:
            fill(device.nid, hostname=device.hostname_hint)
    return len(devices)


def collect_relayed_lan_hints(
    snapshots: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """P1: decode each agent-relayed mDNS/SSDP/WSD capture
    (client/collectors/lan_discovery.py) into the same ``{source: {ip:
    PassiveHint}}`` shape ``scheduler._apply_passive_hints`` already consumes
    for the server's own local passive cycle -- one fill path, two capture
    vantage points. Pure: no DB/network I/O.

    Defense-in-depth: a decoded hint's ip is re-validated RFC1918 here even
    though the agent already filters at capture time (mirrors
    ``persist_agent_routes``) -- a hostile/MITM agent bypasses the client-side
    filter, and a public address must never seed identity enrichment no
    matter what an envelope claims. Deliberately stricter than
    ``passive.parse_relayed_hint``'s own ``_is_local`` check (which also
    allows link-local): this is the last gate before a hint can fill a
    device record, so it narrows to RFC1918-only.
    """
    collected: dict[str, dict[str, Any]] = {}
    for snap in snapshots:
        for record in snap.get("lan_hints") or []:
            hint = passive.parse_relayed_hint(record) if isinstance(record, dict) else None
            if hint is None or not is_rfc1918(hint.ip):
                continue
            collected.setdefault(hint.source, {}).setdefault(hint.ip, hint)
    return collected


AddRouteFn = Callable[..., None]


def persist_agent_routes(
    snapshots: list[dict[str, Any]],
    *,
    add_route: AddRouteFn = db.add_net_route,
) -> int:
    """Persist each agent-reported route (T1) keyed to the REPORTING AGENT's own
    device_nid -- the same identity derivation ``_add_agents`` uses (primary-
    adapter MAC/ip). A different source from the SNMP route-harvest off known
    routers/switches (``scheduler._harvest_infra``), which already writes the
    same ``net_routes`` table; both simply feed the existing ``_route_links``
    L3-edge path (no netmap/template change needed).

    Defense-in-depth: dest/next_hop are re-validated RFC1918 here even though
    the agent already filters them -- a hostile/MITM agent bypasses the client-
    side filter, and a public address must never enter net_routes no matter
    what an envelope claims. Snapshots with no usable agent identity are
    skipped (mirrors ``_add_agents``'s ``nd-unknown`` skip). ``add_route`` is
    injectable for tests.
    """
    written = 0
    for snap in snapshots:
        adapters = [a for a in (snap.get("adapters") or []) if isinstance(a, dict)]
        primary = _primary_adapter(adapters)
        mac = primary.get("mac")
        ipv4 = next((ip for ip in (primary.get("ipv4") or []) if ip), None)
        nid = device_nid(mac=mac, ip=ipv4)
        if nid == "nd-unknown":
            continue  # no usable agent identity -> nothing to key the route to
        for route in snap.get("routes") or []:
            if not isinstance(route, dict):
                continue
            dest, next_hop = route.get("dest"), route.get("next_hop")
            if not is_rfc1918_cidr(dest) or not is_rfc1918(next_hop):
                continue  # defense-in-depth: never trust the agent's own filter alone
            add_route(nid, cidr=dest, next_hop=next_hop, ifindex=route.get("if_index"))
            written += 1
    return written
