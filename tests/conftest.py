"""Shared pytest fixtures and synthetic telemetry payloads.

Two reference machines drive most tests:

* HEALTHY  -- a clean desktop: every score should sit at/near 100 and risk low.
* DEGRADING -- the smoke-test laptop showing many stress signals at once, so
  scores drop and the Bayesian risk lights up (top class: stability or storage).

Payload accessors return deep copies so a test can mutate freely without
leaking state into the next test.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient
from server.config import ServerConfig
from server.main import create_app

HEALTHY_DEVICE = "test-healthy-001"
DEGRADING_DEVICE = "test-degrading-001"


# --------------------------------------------------------------------------- #
# Reference payloads
# --------------------------------------------------------------------------- #
_HEALTHY: dict[str, dict[str, Any]] = {
    "inventory": {
        "hostname": "HEALTHY-DT-01",
        "manufacturer": "Dell Inc.",
        "model": "OptiPlex 7090",
        "chassis": "desktop",
        "os_caption": "Microsoft Windows 11 Pro",
        "os_build": "22631",
        "os_install_date": "2024-01-15",
        "bios_version": "2.1.0",
        "bios_release_date": "2024-06-01",
        "cpu_name": "Intel Core i5-11500",
        "cpu_cores": 6,
        "cpu_logical": 12,
        "total_ram_gb": 16.0,
        "memory_modules": [{"capacity_gb": 16.0, "speed_mhz": 3200, "manufacturer": "Samsung"}],
        "disks": [
            {
                "model": "Samsung SSD 980",
                "media_type": "SSD",
                "size_gb": 512.0,
                "serial_hash": "feedface00001111",
                "interface": "NVMe",
                "bus_type": "NVMe",
            }
        ],
        "driver_problem_count": 0,
        "pending_reboot": False,
    },
    "historical": {
        "reliability_stability_index": 9.6,
        "kernel_power_41_30d": 0,
        "dirty_shutdowns_30d": 0,
        "bugchecks_30d": 0,
        "app_crashes_30d": 0,
        "whea_errors_30d": 0,
        "avg_boot_ms": 21000,
        "storage": [
            {
                "disk": "Samsung SSD 980",
                "media_type": "SSD",
                "wear_pct": 0.0,
                "power_on_hours": 5200,
                "read_errors_total": 0,
                "write_errors_total": 0,
                "temperature_c": 38,
            }
        ],
        "battery": None,
        "observation_days": 30,
    },
    "heartbeat": {
        "cpu_pct": 8.0,
        "cpu_perf_pct": 100.0,
        "mem_avail_mb": 9000.0,
        "committed_pct": 35.0,
        "pagefile_pct": 9.0,
        "disk_read_sec": 0.0,
        "disk_write_sec": 0.0,
        "disk_queue": 0.0,
        "free_space_pct": 61.0,
        "handle_count_total": 40000,
        "nic_errors": 0,
        "user_present": True,
        "uptime_hours": 12.0,
    },
    "events": {"window_hours": 24.0, "events": []},
}

_DEGRADING: dict[str, dict[str, Any]] = {
    "inventory": {
        "hostname": "DEGRADE-LT-01",
        "manufacturer": "Dell Inc.",
        "model": "Latitude 7490",
        "chassis": "laptop",
        "os_caption": "Microsoft Windows 10 Pro",
        "os_build": "19045",
        "os_install_date": "2019-03-01",
        "bios_version": "1.2.3",
        "bios_release_date": "2018-11-20",
        "cpu_name": "Intel Core i7-8650U",
        "cpu_cores": 4,
        "cpu_logical": 8,
        "total_ram_gb": 16.0,
        "memory_modules": [{"capacity_gb": 16.0, "speed_mhz": 2400, "manufacturer": "Micron"}],
        "disks": [
            {
                "model": "SK hynix SSD",
                "media_type": "SSD",
                "size_gb": 256.0,
                "serial_hash": "deadbeefcafe0001",
                "interface": "NVMe",
                "bus_type": "NVMe",
            }
        ],
        "driver_problem_count": 1,
        "pending_reboot": True,
    },
    "historical": {
        "reliability_stability_index": 4.2,
        "kernel_power_41_30d": 4,
        "dirty_shutdowns_30d": 2,
        "bugchecks_30d": 1,
        "app_crashes_30d": 9,
        "whea_errors_30d": 3,
        "avg_boot_ms": 65000,
        "storage": [
            {
                "disk": "SK hynix SSD",
                "media_type": "SSD",
                "wear_pct": 82.0,
                "power_on_hours": 41000,
                "read_errors_total": 0,
                "write_errors_total": 0,
                "temperature_c": 51,
            }
        ],
        "battery": {
            "present": True,
            "design_capacity_mwh": 60000,
            "full_charge_capacity_mwh": 39000,
            "wear_pct": 35.0,
            "cycle_count": 820,
        },
        "observation_days": 30,
    },
    "heartbeat": {
        "cpu_pct": 22.0,
        "cpu_perf_pct": 78.0,
        "mem_avail_mb": 900.0,
        "committed_pct": 92.0,
        "pagefile_pct": 41.0,
        "disk_read_sec": 0.0,
        "disk_write_sec": 0.0,
        "disk_queue": 1.0,
        "free_space_pct": 6.0,
        "handle_count_total": 120000,
        "nic_errors": 0,
        "user_present": True,
        "uptime_hours": 410.0,
    },
    "events": {
        "window_hours": 24.0,
        "events": [
            {
                "ts": "2026-05-28T10:56:23Z",
                "log": "System",
                "source": "Microsoft-Windows-WHEA-Logger",
                "event_id": 17,
                "level": "Warning",
                "message": "A corrected hardware error has occurred.",
            },
            {
                "ts": "2026-05-28T04:53:09Z",
                "log": "System",
                "source": "EventLog",
                "event_id": 6008,
                "level": "Error",
                "message": "The previous system shutdown was unexpected.",
            },
        ],
    },
}


def healthy(msg_type: str) -> dict[str, Any]:
    """Deep copy of a healthy-machine payload for the given message type."""
    return copy.deepcopy(_HEALTHY[msg_type])


def degrading(msg_type: str) -> dict[str, Any]:
    """Deep copy of a degrading-machine payload for the given message type."""
    return copy.deepcopy(_DEGRADING[msg_type])


def envelope(device_id: str, msg_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Build an ingest envelope dict the way the agent's transport would."""
    return {
        "device_id": device_id,
        "agent_version": "0.1.0",
        "msg_type": msg_type,
        "payload": payload,
    }


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """A TestClient backed by a throwaway SQLite DB (fresh per test).

    The ``with`` block runs the app lifespan, which calls ``db.init_db`` and
    points the module-global connection at this test's temp file.
    """
    db_file = tmp_path / "test_srp.db"
    app = create_app(ServerConfig(db_path=str(db_file)))
    with TestClient(app) as c:
        yield c


def _ingest_all(c: TestClient, device_id: str, source) -> None:
    for msg_type in ("inventory", "historical", "heartbeat", "events"):
        resp = c.post("/api/v1/ingest", json=envelope(device_id, msg_type, source(msg_type)))
        assert resp.status_code == 200, resp.text


@pytest.fixture
def seeded_client(client: TestClient) -> TestClient:
    """A client with both reference machines fully ingested (all 4 messages)."""
    _ingest_all(client, HEALTHY_DEVICE, healthy)
    _ingest_all(client, DEGRADING_DEVICE, degrading)
    return client
