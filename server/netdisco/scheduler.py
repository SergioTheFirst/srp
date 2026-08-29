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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, List, Optional

from server import db
from server.analytics import netmap
from server.analytics.oui import normalize_mac, vendor_for_mac
from server.netdisco import adapter_merge, banner, harvest, naming, passive, snmp_probe
from server.netdisco import scan as scan_mod
from server.netdisco.adapters.flow import FlowAdapter
from server.netdisco.adapters.mikrotik import MikroTikAdapter
from server.netdisco.adapters.redfish import RedfishAdapter
from server.netdisco.adapters.unifi import UniFiAdapter
from server.netdisco.classify import classify
from server.netdisco.config import NetdiscoConfig
from server.netdisco.credentials import default_store, resolve_community
from server.netdisco.discovery import gather_candidates
from server.netdisco.drivers import select_driver
from server.netdisco.evidence import collect_lldp_mgmt
from server.netdisco.identity import device_nid, link_identities
from server.netdisco.inventory import (
    build_inventory,
    collect_relayed_lan_hints,
    persist_agent_routes,
    persist_inventory,
)
from server.netdisco.models import DeviceProfile
from server.printers.discovery import is_rfc1918
from server.printers.snmp import SnmpSession

_log = logging.getLogger("srp.netdisco")
_poll_lock = threading.Lock()  # serialize cycles: one inventory/discovery pass at a time

GetSnapshots = Callable[[], list[dict[str, Any]]]
GetKnownFn = Callable[[], list[dict[str, Any]]]
UpsertFn = Callable[[dict[str, Any]], None]
SetLinksFn = Callable[[str, Optional[str], Optional[str]], None]
ScanFn = Callable[..., List[str]]  # cfg, *, on_saturated=... (F6) -- Any-args for injected fakes
PersistRoutesFn = Callable[[list[dict[str, Any]]], int]
CollectHintsFn = Callable[[list[dict[str, Any]]], dict[str, dict]]


def run_inventory_cycle(
    *,
    get_snapshots: GetSnapshots = db.get_network_snapshots,
    upsert: UpsertFn = db.upsert_net_device,
    get_net_devices: GetKnownFn = db.get_net_devices,
    get_printers: GetKnownFn = db.get_printers,
    set_links: SetLinksFn = db.set_net_device_links,
    persist_routes: PersistRoutesFn = persist_agent_routes,
    collect_lan_hints: CollectHintsFn = collect_relayed_lan_hints,
    fill_hints: FillFn = db.fill_net_device_identity,
) -> dict[str, int]:
    """Rebuild + persist the inventory, persist each agent's OWN routing-table
    entries (T1: net_routes -> the existing L3 map-edge path), fold each
    agent's relayed mDNS/SSDP/WSD captures into the same per-field passive
    fill the server's own local passive cycle uses (P1), then FK-link each
    device to its agent / printer record by normalised MAC (Phase 1) -- all
    under one cycle lock.

    Returns ``{"persisted": N, "linked": M, "routes": R, "hints": H, "busy": 0}``
    normally, or the same shape zeroed with ``"busy": 1`` when another cycle
    holds the lock. Dependencies are injectable so tests exercise the cycle
    without the DB/network.
    """
    if not _poll_lock.acquire(blocking=False):
        return {"persisted": 0, "linked": 0, "routes": 0, "hints": 0, "busy": 1}
    try:
        snapshots = get_snapshots()
        devices = build_inventory(snapshots)
        persisted = persist_inventory(devices, upsert=upsert)
        try:
            routes = persist_routes(snapshots)
        except Exception:  # best-effort: a route-persist error must not break the cycle
            _log.exception("agent route persist step failed; persisted inventory is intact")
            routes = 0
        try:
            hints = _apply_relayed_lan_hints(
                snapshots, get_net_devices, collect_lan_hints, fill_hints
            )
        except Exception:  # best-effort: a hint-relay error must not break the cycle
            _log.exception("agent lan-hint relay step failed; persisted inventory is intact")
            hints = 0
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
        return {
            "persisted": persisted,
            "linked": linked,
            "routes": routes,
            "hints": hints,
            "busy": 0,
        }
    finally:
        _poll_lock.release()


def _apply_relayed_lan_hints(
    snapshots: list[dict[str, Any]],
    get_net_devices: GetKnownFn,
    collect_lan_hints: CollectHintsFn,
    fill: FillFn,
) -> int:
    """P1: fold agent-relayed mDNS/SSDP/WSD captures into the exact same
    per-field precedence fill (``_apply_passive_hints``) the server's own
    local passive cycle uses below -- reuse, not a second fill path."""
    collected = collect_lan_hints(snapshots)
    if not collected:
        return 0
    by_ip = {
        dev["ip"]: dev["device_nid"]
        for dev in get_net_devices()
        if dev.get("ip") and dev.get("device_nid") and is_rfc1918(dev["ip"])
    }
    if not by_ip:
        return 0
    return _apply_passive_hints(by_ip, collected, fill)


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
    collect_mgmt_fn: Callable[[str, Any], list] = collect_lldp_mgmt,
    add_route: Optional[Callable[[str, str, str, Optional[int]], None]] = None,
) -> list[tuple]:
    """Passively walk ARP + routes + LLDP mgmt-addrs off each known router/switch ->
    (ip, mac) candidate pairs. A2: persist the full (cidr, next_hop, ifindex) route triple
    via ``add_route`` (was: only next_hop kept). A1: LLDP remote management addresses seed
    discovery (no ping scan). RFC1918-gated, read-only; the mgmt walk is best-effort so one
    bad infra host never breaks the cycle (ARP/route candidates survive a mgmt failure)."""
    pairs: list[tuple] = []
    for dev in devices:
        if dev.get("dev_type") not in _INFRA_TYPES:
            continue
        ip = dev.get("ip")
        if not ip or not is_rfc1918(ip):
            continue
        session = session_factory(ip, cfg)
        pairs.extend(harvest_arp_fn(session))
        nid = dev.get("device_nid")
        for _cidr, next_hop, _ifx in harvest_routes_fn(session):
            pairs.append((next_hop, None))
            if add_route and nid:
                add_route(nid, _cidr, next_hop, _ifx)
        try:
            for _local, mgmt_ip in collect_mgmt_fn(ip, session):
                pairs.append((mgmt_ip, None))
        except Exception:
            _log.debug("lldp mgmt-addr harvest failed for %s", ip, exc_info=True)
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
    add_route: Callable[[str, str, str, Optional[int]], None] = db.add_net_route,
    store_change: Callable[..., None] = db.store_net_change,
) -> dict[str, int]:
    """Active-scan discovery: find live hosts (scan + passive SNMP harvest off known
    routers/switches), merge with ARP/static, persist the NEW ones (serialized by the
    shared lock). No-op unless ``cfg.active_scan``.

    Newly-found hosts are upserted UNKNOWN-first: ``unknown`` when scan-only (no
    MAC), ``endpoint`` when a MAC is known; status ``discovered`` (a later probe/
    classify phase enriches them). Devices already in the inventory are skipped
    entirely so an active sweep can never demote a classified device. All
    dependencies are injectable so tests run without the network/DB.

    F6/MEDIUM-2 (review): a saturated /24 (VPN/proxy on the server host
    answering the whole range) is silently dropped inside ``scan_fn`` --
    ``on_saturated`` makes that visible to the operator too: the dropped range
    count lands in the result as ``"saturated"``, and one ``scan_saturated``
    journal row is written via ``store_change``.
    """
    if not cfg.active_scan:
        return {"discovered": 0, "scanned": 0, "active": 0, "busy": 0}
    if not _poll_lock.acquire(blocking=False):
        return {"discovered": 0, "scanned": 0, "active": 1, "busy": 1}
    try:
        factory = _bind_community(session_factory or _make_session, cfg)
        saturated: dict[str, int] = {"n": 0}

        def _on_saturated(ranges: List[str]) -> None:
            saturated["n"] = len(ranges)
            store_change("scan_saturated", None, {"ranges": list(ranges)})

        scan_ips = tuple(scan_fn(cfg, on_saturated=_on_saturated))
        known_devices = get_known()
        harvest_pairs = _harvest_infra(
            known_devices, cfg, factory, harvest_arp_fn, harvest_routes_fn, add_route=add_route
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
        return {
            "discovered": discovered,
            "scanned": len(scan_ips),
            "active": 1,
            "busy": 0,
            "saturated": saturated["n"],
        }
    finally:
        _poll_lock.release()


# --- Phase 6: classify cycle (probe known hosts -> type + interfaces) -------

_NEEDS_CLASSIFY = frozenset({"unknown", "endpoint"})  # firmly-typed devices are left alone
_SNMP_MUTE_SEC = 24 * 3600  # o5-B6: negative-cache window for a host that stayed silent

AgentMacsFn = Callable[[], set]
ProbeFn = Callable[[str, Any], DeviceProfile]
SessionFactory = Callable[[str, NetdiscoConfig], Any]
StoreInterfacesFn = Callable[[str, List[dict]], None]
SetMuteFn = Callable[[str, Optional[str]], None]


def _fleet_agent_macs() -> set:
    """Every SRP agent's adapter MACs (identity layer) -- never probe our own."""
    return set(netmap.agent_mac_index(db.get_network_snapshots()))


def _make_session(ip: str, cfg: NetdiscoConfig, community: Optional[str] = None) -> SnmpSession:
    resolved = community or resolve_community(cfg, store=default_store())
    return SnmpSession(ip, community=resolved, version=cfg.snmp_version)


def _bind_community(factory: SessionFactory, cfg: NetdiscoConfig) -> SessionFactory:
    """o5-B4: resolve the SNMP community ONCE per cycle (``default_store()`` is
    DPAPI/file I/O) and bind it into the default session factory. An injected/test
    factory -- still the plain 2-arg ``(ip, cfg)`` shape every stub in this codebase
    uses -- is returned untouched, so existing fakes never see a 3rd argument."""
    if factory is not _make_session:
        return factory
    community = resolve_community(cfg, store=default_store())
    return lambda ip, c: _make_session(ip, c, community)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mute_deadline() -> str:
    """o5-B6: 'now + _SNMP_MUTE_SEC' in the same ISO-8601/UTC shape as ``_iso_now()``
    so a plain string comparison against it stays chronologically correct."""
    return (datetime.now(timezone.utc) + timedelta(seconds=_SNMP_MUTE_SEC)).isoformat()


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


# B5 5.2: sysDescr is a free-text banner from an untrusted SNMP-answering host on
# the LAN -- capped on write, well above any real vendor banner, the same "cap at
# the boundary" idiom as the agent-ingest string caps (shared/schema.py).
_SYS_DESCR_MAX = 256


def _device_update(nid: str, profile: DeviceProfile, dev_type: str, extras: dict) -> dict[str, Any]:
    # Review fix (LOW, B5): the sysDescr fallback below is the SAME untrusted banner
    # as the sys_descr field -- cap it here too, once, so the model column can't be
    # used to smuggle an unbounded string past the cap already applied to sys_descr.
    sys_descr = profile.sys_descr[:_SYS_DESCR_MAX] if profile.sys_descr else None
    # review B1-B5 final LOW: the comment above only ever protected the sysDescr
    # FALLBACK -- extras.get("model") (vendor driver) and profile.model_name
    # (ENTITY-MIB entPhysicalModelName, no length bound of its own) still went
    # into net_devices.model raw regardless of which of the three sources won.
    # Cap the picked value ONCE, after the fallback chain, so the comment's
    # claim is actually true for every source, not just the one it was capping.
    model = extras.get("model") or profile.model_name or sys_descr
    if model:
        model = model[:_SYS_DESCR_MAX]
    return {
        "device_nid": nid,
        "dev_type": dev_type,
        "hostname": profile.sys_name,
        "vendor": extras.get("vendor"),  # None -> COALESCE keeps the OUI vendor
        "sys_object_id": profile.sys_object_id,
        # Ф7: prefer a vendor driver's model, then the exact ENTITY model name, then
        # fall back to the verbose sysDescr (UNKNOWN-last ordering).
        "model": model,
        "serial": extras.get("serial") or profile.serial,
        "status": "up" if profile.responded else None,  # None -> keep the prior status
        "sys_descr": sys_descr,
    }


def _classify_targets(devices: List[dict[str, Any]], agent_macs: set) -> List[dict[str, Any]]:
    """Known devices worth an SNMP probe this cycle: a private address, not still
    inside its o5-B6 negative-cache window, not our own agent, and not already
    firmly classified."""
    now = _iso_now()
    targets: List[dict[str, Any]] = []
    for dev in devices:
        ip = dev.get("ip")
        if not ip or not is_rfc1918(ip):
            continue  # need an address, and only ever touch RFC1918 (defense-in-depth)
        mute_until = dev.get("snmp_mute_until")
        if mute_until and mute_until > now:
            continue  # o5-B6: still silent from a recent probe -- skip until it expires
        known_mac = normalize_mac(dev["mac"]) if dev.get("mac") else None
        if known_mac and known_mac in agent_macs:
            continue  # our own machine -> already 'agent' in the inventory
        dev_type_now = dev.get("dev_type") or "unknown"
        if dev_type_now not in _NEEDS_CLASSIFY and dev.get("status") != "discovered":
            continue  # already firmly classified -> don't re-probe, don't demote
        targets.append(dev)
    return targets


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
    set_mute: SetMuteFn = db.set_net_device_snmp_mute,
) -> dict[str, int]:
    """SNMP-probe the not-yet-classified known hosts; set their type + interfaces.

    Gated by ``cfg.enabled`` -- these are unicast probes of already-known RFC1918
    hosts, so the active-scan stop-gate (range scanning) does not apply. Serialized
    by the shared lock. Skips our own agents, already-classified infra (no re-probe,
    no demotion), and hosts still inside their o5-B6 negative-cache window.

    o5-B6: the network probe (session + SNMP GET + driver walk) for each target runs
    in a ``ThreadPoolExecutor`` sized ``min(cfg.scan_workers, len(targets) or 1)``,
    mirroring ``server/netdisco/scan.py``'s ``scan()``. Every DB write (upsert /
    store_interfaces / set_mute) happens afterwards, sequentially, in the calling
    thread -- SQLite has one writer, so nothing but the pure network probe ever runs
    off-thread. A host that answered is un-muted; a silent host is muted for
    ``_SNMP_MUTE_SEC`` so next cycle skips it instead of re-probing it again. All
    dependencies injectable for tests.
    """
    if not cfg.enabled:
        return {"classified": 0, "probed": 0, "busy": 0}
    if not _poll_lock.acquire(blocking=False):
        return {"classified": 0, "probed": 0, "busy": 1}
    try:
        agent_macs = get_agent_macs()
        factory = _bind_community(session_factory, cfg)
        targets = _classify_targets(get_known(), agent_macs)
        ips = [dev["ip"] for dev in targets]

        def probe_one(ip: str) -> Optional[tuple[DeviceProfile, dict]]:
            try:
                session = factory(ip, cfg)
                profile = probe_fn(ip, session)
                extras = select_driver_fn(profile.sys_object_id)(
                    session, sys_object_id=profile.sys_object_id
                )
                return profile, extras
            except Exception:
                # F2: a probe crash is not "silent host" -- that would mute it for
                # _SNMP_MUTE_SEC with the real cause invisible, and upsert a verdict
                # off a fabricated responded=False profile (upsert_net_device's
                # downgrade-guard only protects 'unknown', so an already-known infra
                # device could get demoted). None -> skip entirely, retry next cycle.
                _log.warning("classify probe failed for %s", ip, exc_info=True)
                return None

        workers = min(cfg.scan_workers, len(targets) or 1)
        # ponytail: every target's DeviceProfile/extras is held in memory at once
        # (list(), not a generator) until the DB-write loop below runs -- fine at
        # the scan cap (scan_max_hosts, default 4096 hosts; config.py
        # _DEFAULT_SCAN_MAX_HOSTS). If a genuinely huge segment shows up, chunk
        # targets/ips into groups of workers*4 instead of materializing the whole
        # cycle's results at once -- not worth it at today's ceiling.
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(zip(targets, pool.map(probe_one, ips)))

        probed = 0
        classified = 0
        for dev, outcome in results:
            if outcome is None:
                continue  # probe error -- not counted, retried next cycle
            profile, extras = outcome
            probed += 1
            known_mac = normalize_mac(dev["mac"]) if dev.get("mac") else None
            macs = profile.macs or ((known_mac,) if known_mac else ())
            verdict = classify_fn(replace(profile, macs=macs), agent_macs)
            upsert(_device_update(dev["device_nid"], profile, verdict, extras))
            store_interfaces(dev["device_nid"], _iface_rows(profile))
            set_mute(dev["device_nid"], None if profile.responded else _mute_deadline())
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


# --- Phase 9: optional Tier-3 adapter cycle ---------------------------------
#
# Each configured adapter (operator's controller credentials) is run read-only and
# isolated: a build/collect failure is logged and skipped so one bad controller can
# never block the others, and the merge only enriches/adds by MAC -- it never
# overrides a validated SNMP identity.
_ADAPTER_BUILDERS: dict[str, Any] = {
    "mikrotik": MikroTikAdapter,
    "unifi": UniFiAdapter,
    "redfish": RedfishAdapter,
    "flow": FlowAdapter,
}

MergeFn = Callable[..., dict]
LinkMergeFn = Callable[..., int]


def run_adapter_cycle(
    cfg: NetdiscoConfig,
    *,
    get_known: GetKnownFn = db.get_net_devices,
    merge: MergeFn = adapter_merge.merge_adapter_result,
    link_merge: LinkMergeFn = adapter_merge.merge_adapter_links,
    builders: Optional[dict[str, Any]] = None,
    store: Any = None,
    now: Optional[str] = None,
) -> dict[str, int]:
    """Run each configured optional adapter and merge its identity hints (nodes) AND
    its links into ``net_*`` by MAC (serialized by the shared lock).

    Gated by ``cfg.enabled`` AND a non-empty ``cfg.optional_adapters``. ``known`` is
    re-read per adapter so a node one adapter adds is deduped by the next; link-merge
    re-reads it AFTER node-merge so an endpoint just added is linkable. All deps
    injectable for tests."""
    if not cfg.enabled or not cfg.optional_adapters:
        return {"enriched": 0, "added": 0, "links": 0, "adapters": 0, "busy": 0}
    if not _poll_lock.acquire(blocking=False):
        return {"enriched": 0, "added": 0, "links": 0, "adapters": 0, "busy": 1}
    try:
        build_map = builders if builders is not None else _ADAPTER_BUILDERS
        cred_store = store if store is not None else default_store()
        enriched = added = links = ran = 0
        for acfg in cfg.optional_adapters:
            builder = build_map.get(acfg.adapter_type)
            if builder is None:
                continue  # unimplemented / documented-only type -> skip cleanly
            try:
                result = builder(acfg, store=cred_store).collect()
            except Exception:  # contract says collect() never raises; absorb if it does
                _log.exception("adapter %s build/collect failed", acfg.adapter_type)
                continue
            counts = merge(result, get_known(), now=now)
            enriched += int(counts.get("enriched", 0))
            added += int(counts.get("added", 0))
            # Re-read known AFTER node-merge: a link endpoint the same adapter just
            # added is now resolvable to its canonical nid.
            links += int(link_merge(result, get_known(), adapter_type=acfg.adapter_type, now=now))
            ran += 1
        return {"enriched": enriched, "added": added, "links": links, "adapters": ran, "busy": 0}
    finally:
        _poll_lock.release()
