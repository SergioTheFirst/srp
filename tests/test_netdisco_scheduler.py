"""Phase 4: netdisco inventory scheduler cycle (anti-DoS serialized, injectable).

run_inventory_cycle rebuilds the inventory from current snapshots and persists
it; a single _poll_lock serializes cycles so a mashed force-poll (or the loop
firing mid-poll) returns 'busy' instead of launching a second pass.
"""

from __future__ import annotations

from typing import Any

from server.netdisco import scheduler

_SNAP: dict[str, Any] = {
    "device_id": "dev-A",
    "hostname": "PC-A",
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
    "neighbors": [{"ip": "10.0.0.1", "mac": "00-50-56-AA-BB-CC", "state": "reachable"}],
}


def test_run_inventory_cycle_builds_and_persists() -> None:
    captured: list[dict[str, Any]] = []
    result = scheduler.run_inventory_cycle(get_snapshots=lambda: [_SNAP], upsert=captured.append)
    assert result["busy"] == 0
    # one agent + one agentless gateway endpoint = 2 devices persisted
    assert result["persisted"] == len(captured) == 2


def test_run_inventory_cycle_returns_busy_when_a_cycle_is_running() -> None:
    scheduler._poll_lock.acquire()
    try:
        result = scheduler.run_inventory_cycle(get_snapshots=lambda: [_SNAP], upsert=lambda d: None)
        assert result["busy"] == 1
        assert result["persisted"] == 0
    finally:
        scheduler._poll_lock.release()


# --- Phase 5: active discovery cycle (scan -> gather -> upsert new only) ---

from server.netdisco.config import NetdiscoConfig  # noqa: E402
from server.netdisco.identity import device_nid  # noqa: E402


def test_run_discovery_cycle_is_noop_when_active_scan_off() -> None:
    cfg = NetdiscoConfig(active_scan=False)

    def boom(_: NetdiscoConfig) -> list[str]:
        raise AssertionError("scan ran while active_scan is off")

    result = scheduler.run_discovery_cycle(
        cfg, scan_fn=boom, get_snapshots=lambda: [], get_known=lambda: [], upsert=lambda d: None
    )
    assert result == {"discovered": 0, "scanned": 0, "active": 0, "busy": 0}


def test_run_discovery_cycle_persists_only_new_scan_hosts() -> None:
    cfg = NetdiscoConfig(active_scan=True)
    captured: list[dict[str, Any]] = []
    result = scheduler.run_discovery_cycle(
        cfg,
        scan_fn=lambda c: ["10.0.0.50"],
        get_snapshots=lambda: [],
        get_known=lambda: [],
        upsert=captured.append,
    )
    assert result["active"] == 1 and result["busy"] == 0
    assert result["scanned"] == 1 and result["discovered"] == 1
    assert len(captured) == 1
    dev = captured[0]
    assert dev["ip"] == "10.0.0.50"
    assert dev["dev_type"] == "unknown"  # scan-only host has no MAC -> UNKNOWN-first
    assert dev["status"] == "discovered"


def test_run_discovery_cycle_skips_known_nids_no_demotion() -> None:
    cfg = NetdiscoConfig(active_scan=True)
    known_nid = device_nid(mac=None, ip="10.0.0.50")  # same nid the scan hit would get
    captured: list[dict[str, Any]] = []
    result = scheduler.run_discovery_cycle(
        cfg,
        scan_fn=lambda c: ["10.0.0.50"],
        get_snapshots=lambda: [],
        get_known=lambda: [{"device_nid": known_nid, "dev_type": "router"}],
        upsert=captured.append,
    )
    assert result["discovered"] == 0
    assert captured == []  # a known device is never re-upserted (no router->endpoint demotion)


def test_run_discovery_cycle_returns_busy_when_locked() -> None:
    cfg = NetdiscoConfig(active_scan=True)
    scheduler._poll_lock.acquire()
    try:
        result = scheduler.run_discovery_cycle(
            cfg, scan_fn=lambda c: ["10.0.0.50"], get_snapshots=lambda: [], get_known=lambda: []
        )
        assert result["busy"] == 1 and result["discovered"] == 0
    finally:
        scheduler._poll_lock.release()
