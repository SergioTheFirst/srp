"""Language-independence tests: Russian Windows output must parse without mangling.

Windows localises LevelDisplayName, error text, and product strings, but the
collectors are specifically designed to avoid locale-sensitive paths:

  - Heartbeat/historical: CIM class property names are always English regardless
    of locale (Win32_PerfFormattedData_* uses stable English property names;
    Get-Counter paths are locale-sensitive and are NOT used).
  - Events: $e.Level is mapped numerically in PS (1→'Critical', 2→'Error', ...),
    so the level tag is always English even though LevelDisplayName would be
    'Критическая ошибка' on a Russian system.
  - String fields (hostname, os_caption, manufacturer, message) can legitimately
    contain Cyrillic text; Python must pass them through intact.

These tests pin that invariant so a future refactor cannot accidentally break it.
"""

from __future__ import annotations

import pytest
from client.collectors import events, historical, inventory
from client.collectors.ps import PsResult

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _ok(data):
    return PsResult("ok", data)


def _hist_ps(main_data, cert_data=None):
    _certs = cert_data if cert_data is not None else {"certificates": []}

    def _mock(script, timeout=30):
        return _ok(main_data) if timeout == 120 else _ok(_certs)

    return _mock


# --------------------------------------------------------------------------- #
# Inventory — Cyrillic hostnames and Russian product strings
# --------------------------------------------------------------------------- #

_INV_RUSSIAN = {
    "hostname": "СТАНЦИЯ-01",  # Cyrillic hostname (common in Russian enterprises)
    "manufacturer": "Аквариус",  # Russian OEM manufacturer
    "model": "Aquarius Pro P30 S46",
    "chassis_types": [3],  # desktop
    "os_caption": "Microsoft Windows 10 Профессиональная",  # Russian edition name
    "os_version": "10.0.19045",
    "os_build": "19045",
    "os_install_date": "2023-06-15",
    "bios_version": "1.5.0",
    "bios_release_date": "2022-09-01",
    "cpu_name": "Intel(R) Core(TM) i5-10400 CPU @ 2.90GHz",
    "cpu_cores": 6,
    "cpu_logical": 12,
    "total_ram_bytes": 8_589_934_592,
    "memory_modules": [],
    "disks": [],
    "driver_problem_count": 0,
    "pending_reboot": False,
}


def test_inventory_cyrillic_hostname_passes_through(monkeypatch):
    """Russian hostname 'СТАНЦИЯ-01' must not be stripped or replaced with None."""
    monkeypatch.setattr(inventory, "run_ps", lambda *a, **k: _ok(_INV_RUSSIAN))
    res = inventory.collect_inventory()
    assert res.payload["hostname"] == "СТАНЦИЯ-01"
    assert res.source_health["identity"]["status"] == "ok"


def test_inventory_russian_os_caption_passes_through(monkeypatch):
    """Russian Windows edition name must survive the `or None` coercion."""
    monkeypatch.setattr(inventory, "run_ps", lambda *a, **k: _ok(_INV_RUSSIAN))
    res = inventory.collect_inventory()
    assert res.payload["os_caption"] == "Microsoft Windows 10 Профессиональная"


def test_inventory_cyrillic_manufacturer_passes_through(monkeypatch):
    """Russian OEM manufacturer string must not be treated as falsy."""
    monkeypatch.setattr(inventory, "run_ps", lambda *a, **k: _ok(_INV_RUSSIAN))
    res = inventory.collect_inventory()
    assert res.payload["manufacturer"] == "Аквариус"


# --------------------------------------------------------------------------- #
# Historical — CIM property names are always English (language-independence)
# --------------------------------------------------------------------------- #

_HIST_RUSSIAN_LOCALE = {
    # CIM classes return stable English property names on any locale.
    # The values here are what a Russian Windows box actually produces.
    "reliability_stability_index": 8.2,
    "kernel_power_41_30d": 1,
    "dirty_shutdowns_30d": 0,
    "bugchecks_30d": 0,
    "app_crashes_30d": 3,  # Russian apps crash with Cyrillic paths, but count is int
    "whea_errors_30d": 0,
    "avg_boot_ms": 22_000,
    "storage": [
        {
            # FriendlyName on Russian systems can include Cyrillic brand names
            "disk": "Диск Samsung 860 EVO",
            "media_type": "SSD",
            "wear_pct": 8,
            "power_on_hours": 4380,
            "read_errors_total": 0,
            "write_errors_total": 0,
            "temperature_c": 35,
        }
    ],
    "battery": {"present": False},
    "observation_days": 30,
}


def test_historical_cim_property_names_are_english_on_russian_locale(monkeypatch):
    """CIM property names (reliability_stability_index, etc.) are English on every locale.

    The collector reads them by their English CIM names; this test pins that the
    parser can always find the values regardless of the OS UI language.
    """
    monkeypatch.setattr(historical, "run_ps", _hist_ps(_HIST_RUSSIAN_LOCALE))
    res = historical.collect_historical()
    assert res.payload is not None
    # English CIM key is always present and holds the correct value
    assert res.payload["reliability_stability_index"] == 8.2
    assert res.payload["kernel_power_41_30d"] == 1
    assert res.source_health["reliability"]["status"] == "ok"


def test_historical_cyrillic_disk_name_passes_through(monkeypatch):
    """Disk FriendlyName with Cyrillic text is stored verbatim."""
    monkeypatch.setattr(historical, "run_ps", _hist_ps(_HIST_RUSSIAN_LOCALE))
    res = historical.collect_historical()
    disks = res.payload["storage"]
    assert disks[0]["disk"] == "Диск Samsung 860 EVO"


# --------------------------------------------------------------------------- #
# Events — level is always English; Cyrillic body must not be truncated wrongly
# --------------------------------------------------------------------------- #

# Russian Windows Event Log entries: long Cyrillic message to test len() truncation.
# Python len() counts Unicode code points, not bytes — a Cyrillic char is 1 code point,
# so truncation at 500 chars cuts 500 Cyrillic characters, not 250 (as bytes would).
_EV_LONG_CYRILLIC = {
    "ts": "2024-03-15T03:00:00.0000000Z",
    "log": "System",
    "source": "disk",
    "event_id": 7,
    "level": "Error",  # always English (numeric Level=2 in PS)
    "message": "Ошибка " * 120,  # 840 chars (> 500) — should be truncated to 500
}

_EV_MIXED = {
    "ts": "2024-03-15T04:00:00.0000000Z",
    "log": "Application",
    "source": "Application Error",
    "event_id": 1000,
    "level": "Error",
    "message": "Faulting application: C:\\Program Files\\1С\\1cv8.exe (Ошибка памяти)",
}


def test_events_level_english_on_russian_system(monkeypatch):
    """On Russian Windows, $e.Level=2 is mapped to 'Error' in PS, never 'Ошибка'."""
    raw = {"events": [_EV_LONG_CYRILLIC], "window_hours": 24}
    monkeypatch.setattr(events, "run_ps", lambda *a, **k: _ok(raw))
    res = events.collect_events()
    assert res.payload["events"][0]["level"] == "Error"


def test_events_long_cyrillic_message_truncated_by_codepoints(monkeypatch):
    """Truncation at 500 counts Unicode code points, so 500 Cyrillic chars are kept."""
    raw = {"events": [_EV_LONG_CYRILLIC], "window_hours": 24}
    monkeypatch.setattr(events, "run_ps", lambda *a, **k: _ok(raw))
    res = events.collect_events()
    msg = res.payload["events"][0]["message"]
    assert len(msg) == 500
    # Truncated prefix is still valid Cyrillic text
    assert msg.startswith("Ошибка")


def test_events_mixed_cyrillic_latin_message_passes_through(monkeypatch):
    """Mixed Latin/Cyrillic message (e.g. 1С app crash) is preserved."""
    raw = {"events": [_EV_MIXED], "window_hours": 24}
    monkeypatch.setattr(events, "run_ps", lambda *a, **k: _ok(raw))
    res = events.collect_events()
    msg = res.payload["events"][0]["message"]
    assert "1cv8.exe" in msg
    assert "Ошибка" in msg
