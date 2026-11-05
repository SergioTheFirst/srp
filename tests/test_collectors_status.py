"""Per-source collector_status classification (client/collectors/*).

Each collector's run_ps is monkeypatched to return a synthetic PsResult -- no real
PowerShell. Pins the rule that a collector reports per-source health (and, on a
failed run, marks every owned source with the failure status, payload=None) so the
server never mistakes "source blocked/empty" for "source healthy".
"""

from __future__ import annotations

import pytest
from client.collectors import events, heartbeat, historical, inventory
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
            }
        ),
    )
    res = historical.collect_historical()
    assert res.source_health["storage_reliability"]["status"] == "empty"
    assert res.source_health["reliability"]["status"] == "ok"
    assert res.source_health["boot_time"]["status"] == "ok"


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


def test_inventory_hollow_run_is_identity_empty(monkeypatch):
    """F2: WMI отказал целиком (битый репозиторий, служба остановлена), PowerShell
    отработал -- hostname из $env есть всегда, а модели и ОС нет. Раньше это было
    identity «ok»: пустышка затирала хорошую инвентаризацию и давала ложный trusted."""
    monkeypatch.setattr(
        inventory,
        "run_ps",
        lambda *a, **k: _ok(
            {"hostname": "PC1", "model": "", "os_caption": "", "disks": [], "memory_modules": []}
        ),
    )
    res = inventory.collect_inventory()
    assert res.payload is None
    assert res.source_health["identity"]["status"] == "empty"


def test_inventory_absent_marks_identity(monkeypatch):
    monkeypatch.setattr(inventory, "run_ps", lambda *a, **k: PsResult("absent"))
    res = inventory.collect_inventory()
    assert res.payload is None
    assert res.source_health["identity"]["status"] == "absent"


# --------------------------------------------------------------------------- #
# events
# --------------------------------------------------------------------------- #
def test_events_ok_reports_source(monkeypatch):
    monkeypatch.setattr(
        events, "run_ps", lambda *a, **k: _ok({"events": [{"event_id": 41}], "window_hours": 24.0})
    )
    res = events.collect_events()
    assert res.payload is not None
    assert res.source_health["events"]["status"] == "ok"


def test_events_empty_is_empty(monkeypatch):
    monkeypatch.setattr(events, "run_ps", lambda *a, **k: _ok({"events": [], "window_hours": 24.0}))
    res = events.collect_events()
    assert res.source_health["events"]["status"] == "empty"


def test_events_timeout_marks_source(monkeypatch):
    monkeypatch.setattr(events, "run_ps", lambda *a, **k: PsResult("timeout"))
    res = events.collect_events()
    assert res.payload is None
    assert res.source_health["events"]["status"] == "timeout"


# --------------------------------------------------------------------------- #
# Агент 0.3.5: причина сбоя едет в source_health (статус прежний)
# --------------------------------------------------------------------------- #
def test_broken_powershell_reason_reaches_every_owned_source(monkeypatch):
    broken = PsResult("empty", rc=1, reason="CommandNotFoundException")
    monkeypatch.setattr(historical, "run_ps", lambda *a, **k: broken)
    res = historical.collect_historical()
    assert res.payload is None
    for src in ("storage_reliability", "reliability", "boot_time"):
        assert res.source_health[src]["status"] == "empty"  # доверие не меняется
        assert res.source_health[src]["reason"] == "CommandNotFoundException"


def test_events_failure_carries_reason(monkeypatch):
    monkeypatch.setattr(
        events, "run_ps", lambda *a, **k: PsResult("empty", rc=1, reason="rc_1")
    )
    assert events.collect_events().source_health["events"]["reason"] == "rc_1"


def test_legit_empty_has_no_reason_key(monkeypatch):
    monkeypatch.setattr(events, "run_ps", lambda *a, **k: _ok({"events": [], "arrivals": []}))
    assert "reason" not in events.collect_events().source_health["events"]


def test_inventory_hollow_run_names_cim(monkeypatch):
    monkeypatch.setattr(inventory, "run_ps", lambda *a, **k: _ok({"hostname": "PC-01"}))
    identity = inventory.collect_inventory().source_health["identity"]
    assert (identity["status"], identity["reason"]) == ("empty", "cim_hollow")


def test_health_sanitizes_and_drops_garbage_reason():
    from client.collectors.sources import health

    assert health("empty", "HRESULT 0x80041010")["reason"] == "HRESULT_0x80041010"
    assert "reason" not in health("empty", "бум")
    assert "reason" not in health("ok")
