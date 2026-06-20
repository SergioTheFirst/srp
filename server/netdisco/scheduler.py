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
from typing import Any, Callable

from server import db
from server.netdisco.inventory import build_inventory, persist_inventory

_log = logging.getLogger("srp.netdisco")
_poll_lock = threading.Lock()  # serialize cycles: one inventory pass at a time

GetSnapshots = Callable[[], list[dict[str, Any]]]
UpsertFn = Callable[[dict[str, Any]], None]


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
