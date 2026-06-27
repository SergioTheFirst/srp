"""Server-side netdisco poll scheduler (phase 4).

Phase 4 runs ONE cheap cycle: rebuild the persistent inventory from the agents'
existing network snapshots (no new probes). A single ``_poll_lock`` serializes
cycles -- a second concurrent call (force button mashed, or the loop firing
during a manual poll) returns ``busy`` instead of doing the work twice
(anti-DoS, mirroring the printers scheduler). Active scan / SNMP probe cycles
arrive in later phases and will reuse this lock.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import replace
from typing import Any, Callable, List, Optional

from server import db
from server.analytics import netmap
from server.analytics.oui import normalize_mac, vendor_for_mac
from server.netdisco import banner, harvest, naming, passive, snmp_probe
from server.netdisco import scan as scan_mod
from server.netdisco.classify import classify
from server.netdisco.config import NetdiscoConfig
from server.netdisco.credentials import default_store, resolve_community
from server.netdisco.discovery import gather_candidates
from server.netdisco.drivers import select_driver
from server.netdisco.identity import device_nid, link_identities
from server.netdisco.inventory import build_inventory, persist_inventory
from server.netdisco.models import DeviceProfile
from server.printers.discovery import is_rfc1918
from server.printers.snmp import SnmpSession

_log = logging.getLogger("srp.netdisco")
_poll_lock = threading.Lock()  # serialize cycles: one inventory/discovery pass at a time

GetSnapshots = Callable[[], list[dict[str, Any]]]
GetKnownFn = Callable[[], list[dict[str, Any]]]
UpsertFn = Callable[[dict[str, Any]], None]
SetLinksFn = Callable[[str, Optional[str], Optional[str]], None]
ScanFn = Callable[[NetdiscoConfig], List[str]]


def run_inventory_cycle(
    *,
    get_snapshots: GetSnapshots = db.get_network_snapshots,
    upsert: UpsertFn = db.upsert_net_device,
    get_net_devices: GetKnownFn = db.get_net_devices,
    get_printers: GetKnownFn = db.get_printers,
    set_links: SetLinksFn = db.set_net_device_links,
) -> dict[str, int]:
    """Rebuild + persist the inventory, then FK-link each device to its agent /
    printer record by normalised MAC (Phase 1) -- all under one cycle lock.

    Returns ``{"persisted": N, "linked": M, "busy": 0}`` normally, or
    ``{"persisted": 0, "linked": 0, "busy": 1}`` when another cycle holds the lock.
    Dependencies are injectable so tests exercise the cycle without the DB/network.
    """
    if not _poll_lock.acquire(blocking=False):
        return {"persisted": 0, "linked": 0, "busy": 1}
    try:
        snapshots = get_snapshots()
        devices = build_inventory(snapshots)
        persisted = persist_inventory(devices, upsert=upsert)
        try:
            linked = _link_inventory_identities(
                snapshots,
                get_net_devices=get_net_devices,
                get_printers=get_printers,
                set_links=set_links,
            )
        except Exception:  # link = best-effort enrichment; persisted inventory stays intact
            _log.exception("identity link step failed; persisted inventory is intact")
            linked = 0
        return {"persisted": persisted, "linked": linked, "busy": 0}
    finally:
        _poll_lock.release()


def _link_inventory_identities(
    snapshots: list[dict[str, Any]],
    *,
    get_net_devices: GetKnownFn,
    get_printers: GetKnownFn,
    set_links: SetLinksFn,
) -> int:
    """FK-link the freshly-persisted ``net_devices`` to agent / printer records.

    Join key = normalised agent-adapter MAC (one source of truth,
    ``netmap.agent_mac_index``); IP is the reserve only for MAC-less rows. A
    transient miss never wipes a known FK (COALESCE-preserve in
    ``set_net_device_links``). Returns the number of rows linked."""
    links = link_identities(get_net_devices(), netmap.agent_mac_index(snapshots), get_printers())
    for nid, fk in links.items():
        set_links(nid, fk.get("device_id"), fk.get("printer_id"))
    return len(links)


def poll_now() -> dict[str, int]:
    """Force one inventory cycle now (dashboard button / background loop)."""
    return run_inventory_cycle()


_INFRA_TYPES = frozenset({"router", "switch"})  # devices worth a passive SNMP harvest
HarvestFn = Callable[..., list]


def _harvest_infra(
    devices: list[dict[str, Any]],
    cfg: NetdiscoConfig,
    session_factory: Callable[[str, NetdiscoConfig], Any],
    harvest_arp_fn: HarvestFn,
    harvest_routes_fn: HarvestFn,
) -> list[tuple]:
    """Passively walk ARP + routes off each known router/switch -> (ip, mac) pairs
    (route next-hops carried as (next_hop, None)). RFC1918-gated, read-only; harvest
    helpers never raise (SNMP garbage -> empty), so one bad infra host can't break
    the cycle."""
    pairs: list[tuple] = []
    for dev in devices:
        if dev.get("dev_type") not in _INFRA_TYPES:
            continue
        ip = dev.get("ip")
        if not ip or not is_rfc1918(ip):
            continue
        session = session_factory(ip, cfg)
        pairs.extend(harvest_arp_fn(session))
        pairs.extend((next_hop, None) for _cidr, next_hop, _ifx in harvest_routes_fn(session))
    return pairs


def run_discovery_cycle(
    cfg: NetdiscoConfig,
    *,
    scan_fn: ScanFn = scan_mod.scan,
    get_snapshots: GetSnapshots = db.get_network_snapshots,
    get_known: GetKnownFn = db.get_net_devices,
    upsert: UpsertFn = db.upsert_net_device,
    session_factory: Optional[Callable[[str, NetdiscoConfig], Any]] = None,
    harvest_arp_fn: HarvestFn = harvest.harvest_arp,
    harvest_routes_fn: HarvestFn = harvest.harvest_routes,
) -> dict[str, int]:
    """Active-scan discovery: find live hosts (scan + passive SNMP harvest off known
    routers/switches), merge with ARP/static, persist the NEW ones (serialized by the
    shared lock). No-op unless ``cfg.active_scan``.

    Newly-found hosts are upserted UNKNOWN-first: ``unknown`` when scan-only (no
    MAC), ``endpoint`` when a MAC is known; status ``discovered`` (a later probe/
    classify phase enriches them). Devices already in the inventory are skipped
    entirely so an active sweep can never demote a classified device. All
    dependencies are injectable so tests run without the network/DB.
    """
    if not cfg.active_scan:
        return {"discovered": 0, "scanned": 0, "active": 0, "busy": 0}
    if not _poll_lock.acquire(blocking=False):
        return {"discovered": 0, "scanned": 0, "active": 1, "busy": 1}
    try:
        factory = session_factory or _make_session
        scan_ips = tuple(scan_fn(cfg))
        known_devices = get_known()
        harvest_pairs = _harvest_infra(
            known_devices, cfg, factory, harvest_arp_fn, harvest_routes_fn
        )
        candidates = gather_candidates(
            arp_snapshots=get_snapshots(),
            static_ips=cfg.static_ips,
            scan_ips=scan_ips,
            harvest_arp=harvest_pairs,
        )
        known = {d.get("device_nid") for d in known_devices}
        discovered = 0
        for cand in candidates:
            nid = device_nid(mac=cand.mac, ip=cand.ip)
            if nid == "nd-unknown" or nid in known:
                continue  # unidentifiable, or already known -> never re-upsert (no demotion)
            upsert(
                {
                    "device_nid": nid,
                    "ip": cand.ip,
                    "mac": cand.mac,
                    "vendor": vendor_for_mac(cand.mac),
                    "dev_type": "endpoint" if cand.mac else "unknown",
                    "status": "discovered",
                }
            )
            discovered += 1
        return {"discovered": discovered, "scanned": len(scan_ips), "active": 1, "busy": 0}
    finally:
        _poll_lock.release()


# --- Phase 6: classify cycle (probe known hosts -> type + interfaces) -------

_NEEDS_CLASSIFY = frozenset({"unknown", "endpoint"})  # firmly-typed devices are left alone

AgentMacsFn = Callable[[], set]
ProbeFn = Callable[[str, Any], DeviceProfile]
SessionFactory = Callable[[str, NetdiscoConfig], Any]
StoreInterfacesFn = Callable[[str, List[dict]], None]


def _fleet_agent_macs() -> set:
    """Every SRP agent's adapter MACs (identity layer) -- never probe our own."""
    return set(netmap.agent_mac_index(db.get_network_snapshots()))


def _make_session(ip: str, cfg: NetdiscoConfig) -> SnmpSession:
    community = resolve_community(cfg, store=default_store())
    return SnmpSession(ip, community=community, version=cfg.snmp_version)


def _iface_rows(profile: DeviceProfile) -> List[dict]:
    return [
        {
            "if_index": i.if_index,
            "name": i.name,
            "if_type": i.if_type,
            "speed_mbps": i.speed_mbps,
            "oper_up": i.oper_up,
            "phys_mac": i.phys_mac,
            "if_alias": i.if_alias,
        }
        for i in profile.interfaces
    ]


def _device_update(nid: str, profile: DeviceProfile, dev_type: str, extras: dict) -> dict[str, Any]:
    return {
        "device_nid": nid,
        "dev_type": dev_type,
        "hostname": profile.sys_name,
        "vendor": extras.get("vendor"),  # None -> COALESCE keeps the OUI vendor
        "sys_object_id": profile.sys_object_id,
        # Ф7: prefer a vendor driver's model, then the exact ENTITY model name, then
        # fall back to the verbose sysDescr (UNKNOWN-last ordering).
        "model": extras.get("model") or profile.model_name or profile.sys_descr,
        "serial": extras.get("serial") or profile.serial,
        "status": "up" if profile.responded else None,  # None -> keep the prior status
    }


def run_classify_cycle(
    cfg: NetdiscoConfig,
    *,
    get_known: GetKnownFn = db.get_net_devices,
    get_agent_macs: AgentMacsFn = _fleet_agent_macs,
    probe_fn: ProbeFn = snmp_probe.probe_device,
    session_factory: SessionFactory = _make_session,
    select_driver_fn: Callable[[Optional[str]], Any] = select_driver,
    classify_fn: Callable[[DeviceProfile, set], str] = classify,
    upsert: UpsertFn = db.upsert_net_device,
    store_interfaces: StoreInterfacesFn = db.store_net_interfaces,
) -> dict[str, int]:
    """SNMP-probe the not-yet-classified known hosts; set their type + interfaces.

    Gated by ``cfg.enabled`` -- these are unicast probes of already-known RFC1918
    hosts, so the active-scan stop-gate (range scanning) does not apply. Serialized
    by the shared lock. Skips our own agents and already-classified infra (no
    re-probe, no demotion). All dependencies injectable for tests."""
    if not cfg.enabled:
        return {"classified": 0, "probed": 0, "busy": 0}
    if not _poll_lock.acquire(blocking=False):
        return {"classified": 0, "probed": 0, "busy": 1}
    try:
        agent_macs = get_agent_macs()
        probed = 0
        classified = 0
        for dev in get_known():
            ip = dev.get("ip")
            if not ip or not is_rfc1918(ip):
                continue  # need an address, and only ever touch RFC1918 (defense-in-depth)
            known_mac = normalize_mac(dev["mac"]) if dev.get("mac") else None
            if known_mac and known_mac in agent_macs:
                continue  # our own machine -> already 'agent' in the inventory
            dev_type_now = dev.get("dev_type") or "unknown"
            if dev_type_now not in _NEEDS_CLASSIFY and dev.get("status") != "discovered":
                continue  # already firmly classified -> don't re-probe, don't demote
            session = session_factory(ip, cfg)
            profile = probe_fn(ip, session)
            probed += 1
            macs = profile.macs or ((known_mac,) if known_mac else ())
            verdict = classify_fn(replace(profile, macs=macs), agent_macs)
            extras = select_driver_fn(profile.sys_object_id)(
                session, sys_object_id=profile.sys_object_id
            )
            upsert(_device_update(dev["device_nid"], profile, verdict, extras))
            store_interfaces(dev["device_nid"], _iface_rows(profile))
            classified += 1
        return {"classified": classified, "probed": probed, "busy": 0}
    finally:
        _poll_lock.release()


# --- Phase 8: passive identification (de-anonymise "unknown" nodes) ----------
#
# Lowest-priority enrichment: cross-MAC/printer-map de-anon (offline), reverse-DNS,
# and the multicast/banner collectors fill an EMPTY hostname/subtype/model on a
# node already in inventory -- they never create a node and never overwrite a
# value an agent/SNMP probe established (the writer COALESCEs the stored value).
_PASSIVE_TARGET_CAP = 1024  # bound the unicast fan-out (netbios/reverse-DNS/banner)
_BANNER_CAP = 32  # banner is the slow, sequential, active path (2 touches/host) -> tight ceiling
_BANNER_TIMEOUT = 1.0  # ...and a short per-host TCP budget (held under the poll lock)
# Per-FIELD source precedence (a device-asserted name beats a PTR; a specific
# service class beats NetBIOS's generic "workstation"; an SSDP SERVER beats a banner).
_HOSTNAME_PRIO = ("netbios", "mdns", "reverse_dns", "banner")
_SUBTYPE_PRIO = ("data", "ssdp", "wsd", "mdns", "netbios")
_MODEL_PRIO = ("ssdp", "banner")

FillFn = Callable[..., None]
DictFn = Callable[..., dict]
PrinterMapFn = Callable[[], list]


def _passive_target(dev: dict[str, Any]) -> bool:
    """A node worth a unicast probe: still nameless, or never firmly typed."""
    return (not dev.get("hostname")) or (dev.get("dev_type") in (None, "unknown", "endpoint"))


def _hint_fields(hint: Any) -> dict[str, Optional[str]]:
    """Normalise a source's per-IP value (a bare PTR string, or a PassiveHint) into
    the three fillable fields."""
    if isinstance(hint, str):
        return {"hostname": hint, "subtype": None, "model": None}
    return {
        "hostname": getattr(hint, "hostname", None),
        "subtype": getattr(hint, "subtype", None),
        "model": getattr(hint, "model", None),
    }


def _deanon_from_data(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Ф8 T1: an IP that print traffic maps to a printer queue IS a printer (a
    strong type signal from real data, no network needed)."""
    out: dict[str, Any] = {}
    for row in rows or []:
        ip = row.get("ip")
        if ip and is_rfc1918(ip) and ip not in out:
            out[ip] = passive.PassiveHint(ip=ip, source="data", subtype="printer")
    return out


def _apply_passive_hints(by_ip: dict[str, str], collected: dict[str, dict], fill: FillFn) -> int:
    """Resolve each known node's empty fields from the gathered sources by per-field
    precedence, then fill it. A responder whose IP is not in inventory is ignored."""
    field_prio = (("hostname", _HOSTNAME_PRIO), ("subtype", _SUBTYPE_PRIO), ("model", _MODEL_PRIO))
    enriched = 0
    for ip, nid in by_ip.items():
        fields: dict[str, str] = {}
        for field, prio in field_prio:
            for src in prio:
                source_map = collected.get(src)
                if not source_map or ip not in source_map:
                    continue
                val = _hint_fields(source_map[ip]).get(field)
                if val:
                    fields[field] = val
                    break
        if fields:
            fill(nid, **fields)
            enriched += 1
    return enriched


def run_passive_cycle(
    cfg: NetdiscoConfig,
    *,
    get_known: GetKnownFn = db.get_net_devices,
    fill: FillFn = db.fill_net_device_identity,
    resolve_names_fn: DictFn = naming.resolve_names,
    collect_mdns_fn: DictFn = passive.collect_mdns,
    collect_ssdp_fn: DictFn = passive.collect_ssdp,
    collect_wsd_fn: DictFn = passive.collect_wsd,
    collect_netbios_fn: DictFn = passive.collect_netbios,
    collect_banner_fn: DictFn = banner.collect_banner,
    get_printer_ip_map: PrinterMapFn = db.iter_printer_port_map,
) -> dict[str, int]:
    """De-anonymise nameless nodes from passive/offline sources, filling only empty
    identity fields (serialized by the shared lock).

    Gated by ``cfg.enabled`` AND ``cfg.passive_enabled``; each source is gated by
    membership in ``cfg.passive_protocols``. Only RFC1918 nodes already in inventory
    are ever enriched, the unicast fan-out is capped, and the active banner probe is
    held to a tight ceiling so the cycle cannot starve the other loops on the lock.
    All dependencies injectable for tests."""
    if not (cfg.enabled and cfg.passive_enabled):
        return {"enriched": 0, "busy": 0}
    if not _poll_lock.acquire(blocking=False):
        return {"enriched": 0, "busy": 1}
    try:
        protos = set(cfg.passive_protocols)
        devices = get_known()
        by_ip: dict[str, str] = {}
        targets: list[str] = []
        for dev in devices:
            ip, nid = dev.get("ip"), dev.get("device_nid")
            if not ip or not nid or not is_rfc1918(ip):
                continue
            by_ip.setdefault(ip, nid)  # map ALL known private nodes (multicast hits any)
            if _passive_target(dev) and ip not in targets and len(targets) < _PASSIVE_TARGET_CAP:
                targets.append(ip)  # unicast/reverse-DNS only chase the nameless ones
        collected: dict[str, dict] = {}
        if "data" in protos:
            collected["data"] = _deanon_from_data(get_printer_ip_map())
        if "netbios" in protos:
            collected["netbios"] = collect_netbios_fn(targets)
        if "mdns" in protos:
            collected["mdns"] = collect_mdns_fn()
        if "ssdp" in protos:
            collected["ssdp"] = collect_ssdp_fn()
        if "wsd" in protos:
            collected["wsd"] = collect_wsd_fn()
        if "reverse_dns" in protos:
            collected["reverse_dns"] = resolve_names_fn(targets)
        if "banner" in protos:
            collected["banner"] = collect_banner_fn(
                targets, cap=_BANNER_CAP, timeout=_BANNER_TIMEOUT
            )
        enriched = _apply_passive_hints(by_ip, collected, fill)
        return {"enriched": enriched, "busy": 0}
    finally:
        _poll_lock.release()
