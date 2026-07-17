"""Golden-parser tests: verify collectors parse realistic PS JSON output correctly.

Each test monkeypatches run_ps with a fixture that resembles actual PowerShell
ConvertTo-Json output — integer disk-latency from CIM, int64 sizes, the
single-item-dict-vs-array quirk of ConvertTo-Json — to verify the parse layer's
type coercions and edge-case handling.

Locale invariant (П.3): events carry English level tags regardless of OS locale
because the PS script maps $e.Level numerically (never via LevelDisplayName).
Cyrillic message text from Russian Windows Event Log must pass through intact.
"""

from __future__ import annotations

import pytest
from client.collectors import events, heartbeat, historical, inventory
from client.collectors.ps import PsResult

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _ok(data):
    return PsResult("ok", data)


def _hist_ps(main_data, cert_data=None):
    """Two-call mock for historical: timeout=120 → main data, timeout=60 → certs."""
    _certs = cert_data if cert_data is not None else {"certificates": []}

    def _mock(script, timeout=30):
        if timeout == 120:
            return _ok(main_data)
        return _ok(_certs)

    return _mock


# --------------------------------------------------------------------------- #
# Heartbeat — CIM returns integers where floats are expected
# --------------------------------------------------------------------------- #

_HB_FULL = {
    "cpu_pct": 15,  # integer from Win32_PerfFormattedData_PerfOS_Processor
    "cpu_perf_pct": 67,  # integer from ProcessorInformation CIM class
    "mem_avail_mb": 8192,
    "committed_pct": 45,
    "pagefile_pct": 12,
    # CIM AvgDisksecPerRead/Write are integers: sub-second latency truncates to 0
    "disk_read_sec": 0,
    "disk_write_sec": 0,
    "disk_queue": 0,
    "free_space_pct": 42.3,  # float — result of PS division
    "handle_count_total": 123_456,
    "nic_errors": 2,
    "user_present": True,
    "uptime_hours": 5.2,
}


def test_heartbeat_cim_integers_become_float(monkeypatch):
    """_f() must lift CIM integers to float; integer 0 latency → 0.0, not None."""
    monkeypatch.setattr(heartbeat, "run_ps", lambda *a, **k: _ok(_HB_FULL))
    res = heartbeat.collect_heartbeat()
    p = res.payload
    assert p is not None
    assert isinstance(p["disk_read_sec"], float)
    assert p["disk_read_sec"] == 0.0
    assert isinstance(p["cpu_pct"], float)
    assert p["uptime_hours"] == 5.2


def test_heartbeat_cpu_perf_null_marks_throttle_empty(monkeypatch):
    """Old hardware: ProcessorInformation CIM absent → cpu_perf_pct=None → throttle empty."""
    data = {**_HB_FULL, "cpu_perf_pct": None}
    monkeypatch.setattr(heartbeat, "run_ps", lambda *a, **k: _ok(data))
    res = heartbeat.collect_heartbeat()
    assert res.payload["cpu_perf_pct"] is None
    assert res.source_health["throttle"]["status"] == "empty"
    assert res.source_health["free_space"]["status"] == "ok"
    assert res.source_health["disk_latency"]["status"] == "ok"


def test_heartbeat_read_absent_write_present_satisfies_latency(monkeypatch):
    """disk_latency source uses OR logic: write-only is still ok."""
    data = {**_HB_FULL, "disk_read_sec": None, "disk_write_sec": 0}
    monkeypatch.setattr(heartbeat, "run_ps", lambda *a, **k: _ok(data))
    res = heartbeat.collect_heartbeat()
    assert res.source_health["disk_latency"]["status"] == "ok"


# --------------------------------------------------------------------------- #
# Historical — as_list() quirk, RSI, certificates
# --------------------------------------------------------------------------- #

_STORAGE_ITEM = {
    "disk": "Samsung SSD 860 EVO",
    "media_type": "SSD",
    "wear_pct": 12,
    "power_on_hours": 8760,
    "read_errors_total": 0,
    "write_errors_total": 0,
    "temperature_c": 38,
}

_HIST_BASE = {
    "reliability_stability_index": 7.8,
    "kernel_power_41_30d": 0,
    "dirty_shutdowns_30d": 0,
    "bugchecks_30d": 0,
    "app_crashes_30d": 0,
    "whea_errors_30d": 0,
    "avg_boot_ms": 18_000,
    "observation_days": 30,
}


def test_historical_single_disk_dict_normalized_by_as_list(monkeypatch):
    """ConvertTo-Json can return a dict instead of a list for single-item storage.

    as_list() must wrap it so the parse layer always sees a list.
    """
    raw = {**_HIST_BASE, "storage": _STORAGE_ITEM}
    monkeypatch.setattr(historical, "run_ps", _hist_ps(raw))
    res = historical.collect_historical()
    assert res.payload is not None
    assert isinstance(res.payload["storage"], list)
    assert len(res.payload["storage"]) == 1
    assert res.source_health["storage_reliability"]["status"] == "ok"


def test_historical_multi_disk_list_passthrough(monkeypatch):
    """Two-disk array passes through as-is; both items are preserved."""
    two_disks = [
        _STORAGE_ITEM,
        {**_STORAGE_ITEM, "disk": "WD Blue 1TB", "media_type": "HDD"},
    ]
    raw = {**_HIST_BASE, "storage": two_disks}
    monkeypatch.setattr(historical, "run_ps", _hist_ps(raw))
    res = historical.collect_historical()
    assert len(res.payload["storage"]) == 2


def test_historical_rsi_float_accepted(monkeypatch):
    """Win32_ReliabilityStabilityMetrics returns RSI as float; reliability source = ok."""
    raw = {**_HIST_BASE, "reliability_stability_index": 6.8, "storage": []}
    monkeypatch.setattr(historical, "run_ps", _hist_ps(raw))
    res = historical.collect_historical()
    assert res.payload["reliability_stability_index"] == 6.8
    assert res.source_health["reliability"]["status"] == "ok"


def test_historical_cert_iso_timestamps_preserved(monkeypatch):
    """Certificate not_after/not_before UTC ISO strings survive the cert-script parse."""
    cert_raw = {
        "certificates": [
            {
                "subject": "CN=MyServer, O=Acme Corp",
                "issuer": "CN=Acme Root CA",
                "thumbprint": "ABCDEF1234567890ABCDEF1234567890ABCDEF12",
                "not_after": "2025-12-31T23:59:59.0000000Z",
                "not_before": "2024-01-01T00:00:00.0000000Z",
            }
        ]
    }
    raw = {**_HIST_BASE, "storage": []}
    monkeypatch.setattr(historical, "run_ps", _hist_ps(raw, cert_raw))
    res = historical.collect_historical()
    certs = res.payload["certificates"]
    assert len(certs) == 1
    assert certs[0]["thumbprint"] == "ABCDEF1234567890ABCDEF1234567890ABCDEF12"
    assert certs[0]["not_after"] == "2025-12-31T23:59:59.0000000Z"
    assert res.source_health["certificates"]["status"] == "ok"


# --------------------------------------------------------------------------- #
# Inventory — chassis inference, serial hash, int64 sizes
# --------------------------------------------------------------------------- #

_INV_BASE = {
    "hostname": "WORKPC01",
    "manufacturer": "Dell Inc.",
    "model": "OptiPlex 7090",
    "os_caption": "Microsoft Windows 10 Pro",
    "os_version": "10.0.19045",
    "os_build": "19045",
    "os_install_date": "2022-03-15",
    "bios_version": "1.12.0",
    "bios_release_date": "2023-06-20",
    "cpu_name": "Intel(R) Core(TM) i7-10700 CPU @ 2.90GHz",
    "cpu_cores": 8,
    "cpu_logical": 16,
    "total_ram_bytes": 17_179_869_184,  # 16 GiB as int64
    "memory_modules": [
        {
            "capacity_bytes": 8_589_934_592,  # 8 GiB as int64
            "speed_mhz": 3200,
            "manufacturer": "Samsung",
            "part_number": "M471A1G44AB0-CWE",
        },
    ],
    "driver_problem_count": 0,
    "pending_reboot": False,
}

_DISK_SATA = {
    "model": "Samsung SSD 860 EVO",
    "media_type": "SSD",
    "size_bytes": 256_060_514_304,
    "serial": "WD-12345ABCDE",
    "firmware": "RVT43B6Q",
    "bus_type": "SATA",
}


def _inv_ok(**override):
    data = {**_INV_BASE, **override}
    return _ok(data)


def test_inventory_chassis_type_9_is_laptop(monkeypatch):
    monkeypatch.setattr(inventory, "run_ps", lambda *a, **k: _inv_ok(chassis_types=[9], disks=[]))
    assert inventory.collect_inventory().payload["chassis"] == "laptop"


def test_inventory_chassis_type_3_is_desktop(monkeypatch):
    monkeypatch.setattr(inventory, "run_ps", lambda *a, **k: _inv_ok(chassis_types=[3], disks=[]))
    assert inventory.collect_inventory().payload["chassis"] == "desktop"


def test_inventory_chassis_unknown_code(monkeypatch):
    monkeypatch.setattr(inventory, "run_ps", lambda *a, **k: _inv_ok(chassis_types=[99], disks=[]))
    assert inventory.collect_inventory().payload["chassis"] == "unknown"


def test_inventory_chassis_empty_list_is_unknown(monkeypatch):
    monkeypatch.setattr(inventory, "run_ps", lambda *a, **k: _inv_ok(chassis_types=[], disks=[]))
    assert inventory.collect_inventory().payload["chassis"] == "unknown"


def test_inventory_serial_oem_placeholder_yields_none(monkeypatch):
    """'To be filled by O.E.M.' is a known non-serial → serial_hash = None."""
    disk = {**_DISK_SATA, "serial": "To be filled by O.E.M."}
    monkeypatch.setattr(
        inventory, "run_ps", lambda *a, **k: _inv_ok(chassis_types=[], disks=[disk])
    )
    assert inventory.collect_inventory().payload["disks"][0]["serial_hash"] is None


def test_inventory_serial_zero_string_yields_none(monkeypatch):
    disk = {**_DISK_SATA, "serial": "0"}
    monkeypatch.setattr(
        inventory, "run_ps", lambda *a, **k: _inv_ok(chassis_types=[], disks=[disk])
    )
    assert inventory.collect_inventory().payload["disks"][0]["serial_hash"] is None


def test_inventory_real_serial_yields_16_char_hex(monkeypatch):
    monkeypatch.setattr(
        inventory, "run_ps", lambda *a, **k: _inv_ok(chassis_types=[], disks=[_DISK_SATA])
    )
    h = inventory.collect_inventory().payload["disks"][0]["serial_hash"]
    assert h is not None
    assert len(h) == 16
    assert all(c in "0123456789abcdef" for c in h)


def test_inventory_int64_ram_converted_to_gb(monkeypatch):
    """17 179 869 184 bytes (16 GiB) → ~16.0 total_ram_gb."""
    monkeypatch.setattr(inventory, "run_ps", lambda *a, **k: _inv_ok(chassis_types=[], disks=[]))
    ram = inventory.collect_inventory().payload["total_ram_gb"]
    assert ram is not None
    assert 15.5 < ram < 16.5


def test_inventory_memory_module_capacity_gb(monkeypatch):
    """8 589 934 592 bytes (8 GiB module) → ~8.0 capacity_gb."""
    monkeypatch.setattr(inventory, "run_ps", lambda *a, **k: _inv_ok(chassis_types=[], disks=[]))
    mods = inventory.collect_inventory().payload["memory_modules"]
    assert mods
    assert 7.5 < mods[0]["capacity_gb"] < 8.5


# --------------------------------------------------------------------------- #
# Events — message processing + locale invariant
# --------------------------------------------------------------------------- #

_EV_KP41 = {
    "ts": "2024-03-15T02:31:00.0000000Z",
    "log": "System",
    "source": "Microsoft-Windows-Kernel-Power",
    "event_id": 41,
    "level": "Critical",  # numeric Level=1 mapped in PS, always English
    "message": "The system has rebooted without cleanly shutting down first.",
}

# Russian Windows: message field is Cyrillic; level is still English (PS maps it numerically).
_EV_CYRILLIC = {
    "ts": "2024-03-15T09:00:00.0000000Z",
    "log": "System",
    "source": "disk",
    "event_id": 7,
    "level": "Error",  # numeric Level=2 → 'Error', NOT 'Ошибка' (LevelDisplayName)
    "message": "Обнаружена ошибка устройства \\Device\\Harddisk0\\DR0.",
}


def test_events_kp41_parsed_correctly(monkeypatch):
    raw = {"events": [_EV_KP41], "window_hours": 24}
    monkeypatch.setattr(events, "run_ps", lambda *a, **k: _ok(raw))
    res = events.collect_events()
    evts = res.payload["events"]
    assert len(evts) == 1
    assert evts[0]["event_id"] == 41
    assert evts[0]["level"] == "Critical"
    assert res.source_health["events"]["status"] == "ok"


def test_events_level_always_english_locale_invariant(monkeypatch):
    """PS maps $e.Level numerically → 'Error', never the localized 'Ошибка'."""
    raw = {"events": [_EV_CYRILLIC], "window_hours": 24}
    monkeypatch.setattr(events, "run_ps", lambda *a, **k: _ok(raw))
    res = events.collect_events()
    assert res.payload["events"][0]["level"] == "Error"


def test_events_cyrillic_message_passes_through_intact(monkeypatch):
    """Cyrillic message text from Russian Event Log is preserved without mangling."""
    raw = {"events": [_EV_CYRILLIC], "window_hours": 24}
    monkeypatch.setattr(events, "run_ps", lambda *a, **k: _ok(raw))
    res = events.collect_events()
    msg = res.payload["events"][0]["message"]
    assert "Обнаружена" in msg


def test_events_message_truncated_at_500_chars(monkeypatch):
    long_ev = {**_EV_KP41, "message": "X" * 700}
    raw = {"events": [long_ev], "window_hours": 24}
    monkeypatch.setattr(events, "run_ps", lambda *a, **k: _ok(raw))
    res = events.collect_events()
    assert len(res.payload["events"][0]["message"]) == 500


def test_events_window_hours_integer_coerced_to_float(monkeypatch):
    """PS returns window_hours as int 24; collector must expose it as float 24.0."""
    raw = {"events": [], "window_hours": 24}
    monkeypatch.setattr(events, "run_ps", lambda *a, **k: _ok(raw))
    res = events.collect_events()
    assert isinstance(res.payload["window_hours"], float)
    assert res.payload["window_hours"] == 24.0
