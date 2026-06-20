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
