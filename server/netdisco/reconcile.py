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

from typing import Any, Callable, FrozenSet, List, Optional

from server import db
from server.analytics.oui import normalize_mac
from server.netdisco import fusion as fusion_mod
from server.netdisco.config import NetdiscoConfig
from server.netdisco.evidence import collect_evidence
from server.netdisco.models import NetDevice, NetInterface, ResolvedLink
from server.netdisco.scheduler import _make_session, _poll_lock
from server.printers.discovery import is_rfc1918

# Device types that carry L2 neighbour tables worth probing for topology evidence.
_TOPOLOGY_TYPES = frozenset({"router", "switch", "ap"})


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
    now: Optional[str] = None,
) -> dict[str, int]:
    """Probe known infra for L2 evidence, fuse, and persist the graph (serialized).

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
        replace_links([_link_row(link) for link in links], probed_nids, received_at=now)
        store_snapshot(_graph(devices, links), received_at=now)
        return {"links": len(links), "probed": probed, "busy": 0}
    finally:
        _poll_lock.release()
