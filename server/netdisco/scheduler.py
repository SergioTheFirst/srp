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
from typing import Any, Callable, List

from server import db
from server.analytics.oui import vendor_for_mac
from server.netdisco import scan as scan_mod
from server.netdisco.config import NetdiscoConfig
from server.netdisco.discovery import gather_candidates
from server.netdisco.identity import device_nid
from server.netdisco.inventory import build_inventory, persist_inventory

_log = logging.getLogger("srp.netdisco")
_poll_lock = threading.Lock()  # serialize cycles: one inventory/discovery pass at a time

GetSnapshots = Callable[[], list[dict[str, Any]]]
GetKnownFn = Callable[[], list[dict[str, Any]]]
UpsertFn = Callable[[dict[str, Any]], None]
ScanFn = Callable[[NetdiscoConfig], List[str]]


def run_inventory_cycle(
    *,
    get_snapshots: GetSnapshots = db.get_network_snapshots,
    upsert: UpsertFn = db.upsert_net_device,
) -> dict[str, int]:
    """Rebuild + persist the inventory from current snapshots (serialized).

    Returns ``{"persisted": N, "busy": 0}`` normally, or ``{"persisted": 0,
    "busy": 1}`` when another cycle holds the lock. Dependencies are injectable so
    tests exercise the cycle without the DB/network.
    """
    if not _poll_lock.acquire(blocking=False):
        return {"persisted": 0, "busy": 1}
    try:
        devices = build_inventory(get_snapshots())
        persisted = persist_inventory(devices, upsert=upsert)
        return {"persisted": persisted, "busy": 0}
    finally:
        _poll_lock.release()


def poll_now() -> dict[str, int]:
    """Force one inventory cycle now (dashboard button / background loop)."""
    return run_inventory_cycle()


def run_discovery_cycle(
    cfg: NetdiscoConfig,
    *,
    scan_fn: ScanFn = scan_mod.scan,
    get_snapshots: GetSnapshots = db.get_network_snapshots,
    get_known: GetKnownFn = db.get_net_devices,
    upsert: UpsertFn = db.upsert_net_device,
) -> dict[str, int]:
    """Active-scan discovery: find live hosts, merge with ARP/static, persist the
    NEW ones (serialized by the shared lock). No-op unless ``cfg.active_scan``.

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
        scan_ips = tuple(scan_fn(cfg))
        candidates = gather_candidates(
            arp_snapshots=get_snapshots(),
            static_ips=cfg.static_ips,
            scan_ips=scan_ips,
        )
        known = {d.get("device_nid") for d in get_known()}
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
