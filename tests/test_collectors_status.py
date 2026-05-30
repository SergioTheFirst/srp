"""Per-source collector_status classification (client/collectors/*).

Each collector's run_ps is monkeypatched to return a synthetic PsResult -- no real
PowerShell. Pins the rule that a collector reports per-source health (and, on a
failed run, marks every owned source with the failure status, payload=None) so the
server never mistakes "source blocked/empty" for "source healthy".
"""

from __future__ import annotations

import pytest
from client.collectors import heartbeat, historical, inventory
from client.collectors.ps import PsResult

pytestmark = pytest.mark.unit


def _ok(data):
    return PsResult("ok", data)


# --------------------------------------------------------------------------- #
# heartbeat
# --------------------------------------------------------------------------- #
def test_heartbeat_ok_reports_all_sources(monkeypatch):
    monkeypatch.setattr(
        heartbeat,
        "run_ps",
        lambda *a, **k: _ok({"cpu_perf_pct": 90.0, "free_space_pct": 50.0, "disk_read_sec": 0.01}),
    )
    res = heartbeat.collect_heartbeat()
    assert res.payload is not None
    assert res.source_health["free_space"]["status"] == "ok"
    assert res.source_health["throttle"]["status"] == "ok"
    assert res.source_health["disk_latency"]["status"] == "ok"
    assert res.source_health["free_space"]["collected_at"] is not None


def test_heartbeat_timeout_marks_all_sources(monkeypatch):
    monkeypatch.setattr(heartbeat, "run_ps", lambda *a, **k: PsResult("timeout"))
    res = heartbeat.collect_heartbeat()
    assert res.payload is None
    assert {v["status"] for v in res.source_health.values()} == {"timeout"}


def test_heartbeat_missing_field_is_empty(monkeypatch):
    monkeypatch.setattr(heartbeat, "run_ps", lambda *a, **k: _ok({"cpu_perf_pct": 90.0}))
    res = heartbeat.collect_heartbeat()
    assert res.source_health["free_space"]["status"] == "empty"
    assert res.source_health["throttle"]["status"] == "ok"


# --------------------------------------------------------------------------- #
# historical
# --------------------------------------------------------------------------- #
def test_historical_empty_storage_is_empty(monkeypatch):
    monkeypatch.setattr(
        historical,
        "run_ps",
        lambda *a, **k: _ok(
            {
                "storage": [],
                "reliability_stability_index": 9.0,
                "avg_boot_ms": 20000,
                "battery": {"present": False},
            }
        ),
    )
    res = historical.collect_historical()
    assert res.source_health["storage_reliability"]["status"] == "empty"
    assert res.source_health["reliability"]["status"] == "ok"
    assert res.source_health["boot_time"]["status"] == "ok"
    assert res.source_health["battery"]["status"] == "ok"


def test_historical_rsi_none_but_counts_is_partial(monkeypatch):
    monkeypatch.setattr(
        historical,
        "run_ps",
        lambda *a, **k: _ok(
            {
                "storage": [{"wear_pct": 1}],
                "reliability_stability_index": None,
                "kernel_power_41_30d": 2,
                "avg_boot_ms": None,
                "battery": {"present": False},
            }
        ),
    )
    res = historical.collect_historical()
    assert res.source_health["reliability"]["status"] == "partial"
    assert res.source_health["storage_reliability"]["status"] == "ok"
    assert res.source_health["boot_time"]["status"] == "empty"


def test_historical_blocked_marks_all_sources(monkeypatch):
    monkeypatch.setattr(historical, "run_ps", lambda *a, **k: PsResult("blocked"))
    res = historical.collect_historical()
    assert res.payload is None
    assert {v["status"] for v in res.source_health.values()} == {"blocked"}


# --------------------------------------------------------------------------- #
# inventory
# --------------------------------------------------------------------------- #
def test_inventory_ok_reports_identity(monkeypatch):
    monkeypatch.setattr(
        inventory,
        "run_ps",
        lambda *a, **k: _ok(
            {"hostname": "PC1", "model": "OptiPlex", "disks": [], "memory_modules": []}
        ),
    )
    res = inventory.collect_inventory()
    assert res.payload["hostname"] == "PC1"
    assert res.source_health["identity"]["status"] == "ok"


def test_inventory_absent_marks_identity(monkeypatch):
    monkeypatch.setattr(inventory, "run_ps", lambda *a, **k: PsResult("absent"))
    res = inventory.collect_inventory()
    assert res.payload is None
    assert res.source_health["identity"]["status"] == "absent"
