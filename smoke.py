"""In-process end-to-end smoke test (no network, no PowerShell).

Boots the FastAPI app against a throwaway SQLite DB, pushes one synthetic
envelope of every message type for a fake "degrading laptop", and asserts the
ingest pipeline computes scores and both dashboard pages render. This is the
fast, deterministic check; the real-machine end-to-end lives in tests/ + a live
agent run. Usage::

    python smoke.py        # exits 0 on success, 1 on any failure
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from server.config import ServerConfig
from server.main import create_app

DEVICE = "smoke-device-001"


def _env(msg_type: str, payload: dict) -> dict:
    return {
        "device_id": DEVICE,
        "agent_version": "0.1.0",
        "msg_type": msg_type,
        "payload": payload,
    }


# A laptop showing several stress signals so the scores are non-trivial.
INVENTORY = {
    "hostname": "SMOKE-LT-01",
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
}

HISTORICAL = {
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
}

HEARTBEAT = {
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
}

EVENTS = {
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
}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="srp_smoke_")) / "smoke.db"
    app = create_app(ServerConfig(db_path=str(tmp)))
    failures: list[str] = []

    with TestClient(app) as client:
        if client.get("/api/v1/health").json().get("status") != "ok":
            failures.append("health endpoint not ok")

        for msg_type, payload in (
            ("inventory", INVENTORY),
            ("historical", HISTORICAL),
            ("heartbeat", HEARTBEAT),
            ("events", EVENTS),
        ):
            resp = client.post("/api/v1/ingest", json=_env(msg_type, payload))
            print(f"ingest {msg_type:10} -> HTTP {resp.status_code}")
            if resp.status_code != 200:
                failures.append(f"ingest {msg_type} -> {resp.status_code}: {resp.text[:200]}")

        device = client.get(f"/api/v1/devices/{DEVICE}").json()
        scores = device.get("scores") or {}
        summary = {
            k: scores.get(k) for k in ("performance", "reliability", "wear", "risk_exposure")
        }
        print("scores:", summary)
        if any(summary[k] is None for k in summary):
            failures.append(f"missing scores: {summary}")
        risk = scores.get("risk") or {}
        print(f"top risk class: {risk.get('top')} = {risk.get('overall')}")

        fleet = client.get("/")
        detail = client.get(f"/device/{DEVICE}")
        print(f"dashboard: fleet HTTP {fleet.status_code}, detail HTTP {detail.status_code}")
        if fleet.status_code != 200:
            failures.append("fleet page did not render")
        if detail.status_code != 200 or "SMOKE-LT-01" not in detail.text:
            failures.append("device detail page missing or hostname not rendered")

    if failures:
        print("\nSMOKE FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nSMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
