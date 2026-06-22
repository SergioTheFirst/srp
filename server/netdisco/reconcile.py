"""Phase 9 -- §4.5 topology reconcile cycle: evidence -> fuse -> persist.

Each cycle walks the known infra devices (router/switch/AP -- the ones that hold
LLDP/CDP/FDB tables), collects link evidence, fuses it into one deterministic graph
(:mod:`server.netdisco.fusion`), then persists it:

* ``replace_net_links`` for the *probed* nodes -- re-derives their links and drops
  vanished ones, while links between un-probed nodes are left untouched; a rerun
  never duplicates (idempotent).
* an append-only ``net_topology_snapshots`` row (graph history).
* ``upsert_net_device`` to advance ``last_seen`` for each reachable infra device.

Read-only SNMP only (the collectors never SET), RFC1918-gated, serialized by the
shared poll lock, and self-contained: a transient per-host error is swallowed by the
collectors (garbage -> empty), so one bad device cannot break the cycle.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, FrozenSet, List, Optional

from server import db
from server.analytics.oui import normalize_mac
from server.netdisco import changes, correlation
from server.netdisco import fusion as fusion_mod
from server.netdisco import scan as scan_mod
from server.netdisco.config import NetdiscoConfig
from server.netdisco.credentials import default_store, resolve_community
from server.netdisco.evidence import collect_evidence
from server.netdisco.graph import build_graph
from server.netdisco.metrics import METRICS
from server.netdisco.models import NetDevice, NetInterface, ResolvedLink
from server.netdisco.scheduler import _make_session, _poll_lock
from server.printers.discovery import is_rfc1918

# Device types that carry L2 neighbour tables worth probing for topology evidence.
_TOPOLOGY_TYPES = frozenset({"router", "switch", "ap"})
# Ghost lifecycle (§3.13): a device is "missing" after this many idle cycles, and
# "eligible_purge" after a long absence -- never on a single missed cycle.
_STALE_CYCLES = 3
_PURGE_AFTER_SEC = 30 * 86400  # 30 days, matching the agent-device ghost sweep


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _infra_macs(devices: List[dict[str, Any]]) -> FrozenSet[str]:
    """Normalised MACs of the known infra devices -- lets FDB inference tell an
    uplink (a port that sees a switch) from an edge (a port that sees one host)."""
    out = set()
    for dev in devices:
        if dev.get("dev_type") in _TOPOLOGY_TYPES and dev.get("mac"):
            mac = normalize_mac(dev["mac"])
            if mac:
                out.add(mac)
    return frozenset(out)


def _to_netdevice(dev: dict[str, Any]) -> NetDevice:
    """A device row (optionally with its interfaces) -> the model the collector
    needs. Only nid/mac/interface-MACs matter (own-MAC filtering in §4.3)."""
    ifaces = tuple(
        NetInterface(phys_mac=row.get("phys_mac"))
        for row in (dev.get("interfaces") or [])
        if row.get("phys_mac")
    )
    return NetDevice(nid=dev["device_nid"], ip=dev.get("ip"), mac=dev.get("mac"), interfaces=ifaces)


def _link_row(link: ResolvedLink) -> dict[str, Any]:
    return {
        "a_nid": link.a,
        "b_nid": link.b,
        "link_kind": link.link_kind,
        "via_source": link.via_source,
        "confidence": link.confidence,
    }


def _graph(devices: List[dict[str, Any]], links: List[ResolvedLink]) -> dict[str, Any]:
    return {
        "nodes": [
            {
                "nid": d.get("device_nid"),
                "dev_type": d.get("dev_type"),
                "ip": d.get("ip"),
                "hostname": d.get("hostname"),
            }
            for d in devices
        ],
        "links": [
            {
                "a": link.a,
                "b": link.b,
                "via_source": link.via_source,
                "confidence": link.confidence,
                "link_kind": link.link_kind,
                "ambiguous": link.ambiguous,
            }
            for link in links
        ],
    }


def run_topology_cycle(
    cfg: NetdiscoConfig,
    *,
    get_known: Callable[[], List[dict[str, Any]]] = db.get_net_devices,
    get_device: Callable[[str], Optional[dict[str, Any]]] = db.get_net_device,
    session_factory: Callable[[str, NetdiscoConfig], Any] = _make_session,
    collect: Callable[..., List] = collect_evidence,
    fuse: Callable[[List], List[ResolvedLink]] = fusion_mod.fuse,
    replace_links: Callable[..., None] = db.replace_net_links,
    store_snapshot: Callable[..., None] = db.store_topology_snapshot,
    upsert: Callable[..., None] = db.upsert_net_device,
    get_prev_snapshot: Callable[[], Optional[dict]] = db.get_latest_topology_snapshot,
    store_change: Callable[..., None] = db.store_net_change,
    set_status: Callable[[str, str], None] = db.set_net_device_status,
    now: Optional[str] = None,
) -> dict[str, int]:
    """Probe known infra for L2 evidence, fuse, persist the graph + change journal,
    and age out ghosts (serialized).

    Gated by ``cfg.enabled``; returns ``busy=1`` if another cycle holds the lock.
    Only RFC1918 router/switch/AP devices are probed. All dependencies injectable."""
    if not cfg.enabled:
        return {"links": 0, "probed": 0, "busy": 0}
    if not _poll_lock.acquire(blocking=False):
        return {"links": 0, "probed": 0, "busy": 1}
    try:
        devices = get_known()
        infra_macs = _infra_macs(devices)
        evidence: List = []
        probed_nids: set = set()
        probed = 0
        for dev in devices:
            if dev.get("dev_type") not in _TOPOLOGY_TYPES:
                continue
            ip = dev.get("ip")
            if not ip or not is_rfc1918(ip):
                continue  # only ever touch private infra (defense-in-depth)
            netdev = _to_netdevice(get_device(dev["device_nid"]) or dev)
            session = session_factory(ip, cfg)
            evidence.extend(collect(netdev, session, infra_macs=infra_macs))
            probed_nids.add(netdev.nid)
            probed += 1
            upsert({"device_nid": netdev.nid, "status": "up"}, now)  # advance last_seen
        links = fuse(evidence)
        new_graph = _graph(devices, links)
        prev_graph = (get_prev_snapshot() or {}).get("graph") or {"nodes": [], "links": []}
        deltas = changes.diff(prev_graph, new_graph)
        replace_links([_link_row(link) for link in links], probed_nids, received_at=now)
        store_snapshot(new_graph, received_at=now)
        for delta in deltas:
            store_change(delta.kind, delta.device_nid, delta.detail, now)
        aged = changes.stale_lifecycle(
            devices,
            now=now or _iso_now(),
            stale_after_sec=_STALE_CYCLES * cfg.topology_interval_sec,
            purge_after_sec=_PURGE_AFTER_SEC,
        )
        for nid, status in aged:
            set_status(nid, status)
        METRICS.observe_cycle("topology", probed=probed, links=len(links), deltas=len(deltas))
        return {"links": len(links), "probed": probed, "deltas": len(deltas), "busy": 0}
    finally:
        _poll_lock.release()


# Vantage points the monitor trusts as "up" when correlating reachability.
_ROOT_TYPES = frozenset({"agent", "router"})


def run_reachability_cycle(
    cfg: NetdiscoConfig,
    *,
    get_known: Callable[[], List[dict[str, Any]]] = db.get_net_devices,
    get_links: Callable[[], List[dict[str, Any]]] = db.get_net_links,
    is_alive: Callable[..., bool] = scan_mod.host_is_alive,
    set_status: Callable[[str, str], None] = db.set_net_device_status,
    store_change: Callable[..., None] = db.store_net_change,
    now: Optional[str] = None,
) -> dict[str, int]:
    """Ping known RFC1918 devices, correlate failures into DOWN vs UNREACHABLE.

    A device whose path to a root (agent/router) crosses another down device is
    UNREACHABLE (suppressed); the upstream failure is the single root cause raised.
    Gated by ``cfg.enabled``, serialized by the shared lock, read-only liveness only.
    A device that answers again is returned to ``up``. All dependencies injectable."""
    if not cfg.enabled:
        return {"down": 0, "unreachable": 0, "busy": 0}
    if not _poll_lock.acquire(blocking=False):
        return {"down": 0, "unreachable": 0, "busy": 1}
    try:
        community = resolve_community(cfg, store=default_store())
        devices = get_known()
        down_set: set = set()
        live_nids: set = set()
        for dev in devices:
            ip, nid = dev.get("ip"), dev.get("device_nid")
            if not ip or not nid or not is_rfc1918(ip):
                continue  # only ever probe private hosts
            alive = is_alive(
                ip, ports=cfg.scan_ports, community=community, version=cfg.snmp_version
            )
            (live_nids if alive else down_set).add(nid)
        graph = build_graph(devices, get_links())
        roots = {
            d["device_nid"]
            for d in devices
            if d.get("dev_type") in _ROOT_TYPES and d.get("device_nid")
        }
        verdicts = correlation.correlate(graph, down_set, roots)
        down = unreachable = 0
        for nid, verdict in verdicts.items():
            set_status(nid, verdict.status)
            if verdict.status == correlation.DOWN:
                down += 1
                store_change("root_cause", nid, {"status": correlation.DOWN}, now)
            else:
                unreachable += 1
        prior = {d.get("device_nid"): d.get("status") for d in devices}
        for nid in live_nids:  # a device that answers again recovers to up
            if prior.get(nid) in (correlation.DOWN, correlation.UNREACHABLE, changes.MISSING):
                set_status(nid, "up")
        METRICS.observe_cycle("reachability", down=down, unreachable=unreachable)
        return {"down": down, "unreachable": unreachable, "busy": 0}
    finally:
        _poll_lock.release()
