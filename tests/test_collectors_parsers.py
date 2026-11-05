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

import re

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


def test_boot_time_loop_guards_each_event() -> None:
    """o5-C14: одно событие Id=100 с битым BootTime не должно убивать avg_boot_ms."""
    from client.collectors.historical import _SCRIPT

    head, tail = _SCRIPT.split("foreach ($e in $btEvents)")
    assert not head.rstrip().endswith("try {"), "внешний try вокруг цикла загрузки остался"
    assert "} catch { continue }" in tail.split("$avgBoot")[0]
    # Само перечисление журнала остаётся под защитой: его падение уносит весь конверт.
    assert "try { $btEvents = @(Get-WinEvent" in _SCRIPT


def test_events_grab_guards_each_event() -> None:
    """Одно битое событие не должно обрезать остаток выборки Grab().

    ``$e.TimeCreated.ToUniversalTime()`` бросает на событии с пустым TimeCreated,
    ``$e.Message`` и ``[int]$e.Id`` -- на повреждённой записи. При try/catch вокруг
    ВСЕГО цикла такая запись молча отсекает все следующие за ней события -- то есть
    основные улики по BSOD/крашам (§5: try/catch на КАЖДЫЙ элемент цикла).
    """
    from client.collectors.events import _SCRIPT

    head, tail = _SCRIPT.split("foreach ($e in $records)")
    assert not head.rstrip().endswith("try {"), "внешний try вокруг цикла Grab остался"
    assert "} catch { continue }" in tail.split("return $out")[0]
    # Перечисление журнала -- под своей защитой (журнала может не быть на этой SKU).
    assert "try { $records = @(Get-WinEvent" in _SCRIPT


# --------------------------------------------------------------------------- #
# Агент 0.3.4, пачка B: сборщики + съёмные устройства вне диагноза
# --------------------------------------------------------------------------- #


def test_inventory_pending_reboot_ignores_pending_file_renames() -> None:
    """Находка 3: хвосты EdgeUpdate в PendingFileRenameOperations -- не «зависшие обновления».

    Fast Startup их не разбирает: флаг висел бы почти на каждом ПК до настоящего рестарта.
    """
    s = inventory._SCRIPT
    assert "PendingFileRenameOperations" not in s
    assert "Component Based Servicing\\RebootPending" in s
    assert "Auto Update\\RebootRequired" in s


def test_inventory_driver_count_skips_disabled_and_peripheral_devices() -> None:
    """Находка 12 + USB: код 22 (отключено вручную) и периферия ПО ПРИРОДЕ -- не «ошибка
    драйвера». Флаг InLocalMachineContainer не годится: на ноутбуках он $false и у встроенных
    USB-камеры/BT-модуля/картридера (ревью, проверено живьём) -- их сбои считаются, как раньше.
    Сбой перечисления -> null, не «0 проблем»."""
    s = inventory._SCRIPT
    assert "ConfigManagerErrorCode <> 0 AND ConfigManagerErrorCode <> 22" in s
    assert "DEVPKEY_Device_InLocalMachineContainer" not in s and "GetDeviceProperties" not in s
    assert "$drv = $null" in s and "-ErrorAction Stop" in s
    found = re.search(r"\$periph = '([^']+)'", s)
    assert found is not None
    periph = re.compile(found.group(1))  # .NET-регулярка из скрипта, Python читает её так же
    excluded = [
        r"SWD\PRINTENUM\{1234}",
        r"SWD\IPP\x",
        r"SWD\ESCL\x",
        r"SWD\DAFWSDPROVIDER\x",
        r"BTHENUM\{0000110E}",
        r"BTHLE\DEV_E8B5",
        r"BTHLEDEVICE\{1800}",
        r"USBSTOR\DISK&VEN_X",
        r"SWD\WPDBUSENUM\_??_USBSTOR#DISK&VEN_X&PROD_Y&REV_1#SER&0#{53F56307-B6BF-11D0-94F2}",
        r"SWD\WPDBUSENUM\{3120354D-88C5-11F0-88B8-E0752663F12D}#0000000000100000",
    ]
    counted = [
        r"USB\VID_0BDA&PID_5830\5&1",  # встроенная веб-камера
        r"BTH\MS_BTHLE\7&1",  # дочерний узел встроенного BT-модуля
        r"HID\INTC816\3&1",
        r"PCI\VEN_10EC&DEV_C822",
        r"SWD\MMDEVAPI\x",
        r"SWD\WPDBUSENUMX\y",  # слеш после имени перечислителя -- граница имени
        "",
    ]
    assert all(periph.match(pnp) for pnp in excluded)
    assert not any(periph.match(pnp) for pnp in counted)


def test_boot_time_is_median_of_normal_boots() -> None:
    """Находка 6: одна долгая загрузка после обновления тянула среднее вверх.

    Медиана загрузок с BootIsRebootAfterInstall = false (xs:boolean -> 'false', не
    локализуется); нет таких -- медиана всех: boot_time -- источник доверия, не EMPTY.
    """
    s = historical._SCRIPT
    assert "Measure-Object -Average" not in s
    loop = s.split("foreach ($e in $btEvents)")[1].split("$avgBoot")[0]
    assert "'BootIsRebootAfterInstall' }).'#text' -eq 'false'" in loop
    assert "} catch { continue }" in loop


def test_events_cap_keeps_evidence_priority_before_time_sort() -> None:
    """Находка 11: всплеск WHEA/падений программ не должен вытеснять BugCheck 1001."""
    s = events._SCRIPT
    block = s.split("$queries = @(")[1].split("$all = @()")[0]
    order = [
        "Id=41;",
        "Id=6008;",
        "Id=1001;",
        "ProviderName='disk'; Id=7,",
        "ProviderName='disk'; Id=157;",
        "'storahci'",
        "'stornvme'",
        "'Ntfs'",
        "WHEA-Logger",
        "WindowsUpdateClient",
        "'Application Error'",
        "'Application Hang'",
    ]
    pos = [block.index(t) for t in order]
    assert pos == sorted(pos)
    assert s.index("Select-Object -First 120") < s.index("Sort-Object { $_.ts } -Descending")


def test_events_disk_bus_lookup_reads_numbers_only() -> None:
    """USB: номер диска -- из пути \\Device\\HarddiskN\\ события, шина -- из журнала 1006.

    Модель, серийник и MBR из записи 1006 не читаются; перечисление и каждая запись
    -- под своей защитой.
    """
    s = events._SCRIPT
    assert r"-match '\\Device\\Harddisk(\d+)\\'" in s
    assert "LogName='Microsoft-Windows-Partition/Diagnostic'; Id=1006} -MaxEvents 200" in s
    for leak in ("SerialNumber", "Model", "Mbr"):
        assert leak not in s
    head, loop = s.split("foreach ($r in $recs)")
    assert "try { $recs = @(Get-WinEvent" in head
    assert "} catch { continue }" in loop
    # Живой бокс: 10 из 200 записей 1006 (дешёвые флешки, 0x1F/0x06 в серийнике) роняют
    # [xml] -- ровно те, что нужны. Поля -- по имени регэкспом, только цифры.
    assert "[xml]" not in loop
    assert r"<Data Name='DiskNumber'>(\d+)</Data>" in loop
    assert r"<Data Name='BusType'>(\d+)</Data>" in loop


_EV_DISK1 = {
    "ts": "2026-08-16T13:38:59.3469747Z",
    "log": "System",
    "source": "disk",
    "event_id": 51,
    "level": "Warning",
    "message": "An error was detected on device \\Device\\Harddisk1\\DR13.",
    "disk": 1,
}


def _arr(ts: str, disk: int, bus: int) -> dict:
    return {"ts": ts, "disk": disk, "bus": bus}


def _events_with(monkeypatch, evs, arrivals):
    raw = {"events": evs, "arrivals": arrivals, "window_hours": 24}
    monkeypatch.setattr(events, "run_ps", lambda *a, **k: _ok(raw))
    return events.collect_events().payload["events"]


@pytest.mark.parametrize("bus", [7, 4])  # STORAGE_BUS_TYPE: 7 = USB, 4 = 1394
def test_events_disk_on_removable_bus_becomes_disk_removable(monkeypatch, bus):
    """Запись 1006 через 8 мс ПОСЛЕ события (так на живом боксе) не в счёт -- решает прежняя."""
    arrivals = [
        _arr("2026-08-16T13:38:59.3551304Z", 1, 11),
        _arr("2026-08-16T13:38:50.0602425Z", 1, bus),
        _arr("2026-08-16T13:38:55.0000000Z", 0, 17),
    ]
    out = _events_with(monkeypatch, [_EV_DISK1], arrivals)
    assert out[0]["source"] == "disk-removable"  # без «:» -- ключ свёртки делится по нему
    assert "disk" not in out[0]  # служебный номер диска на сервер не уходит


@pytest.mark.parametrize(
    "event, arrivals",
    [
        (_EV_DISK1, None),  # журнал 1006 не прочитан / пуст
        (_EV_DISK1, [_arr("2026-08-16T12:00:00.0000000Z", 1, 17)]),  # диск 1 -- NVMe
        (_EV_DISK1, [_arr("2026-08-16T13:39:04.0000000Z", 1, 7)]),  # флешка -- позже события
        (_EV_DISK1, [_arr("2026-08-16T12:00:00.0000000Z", 2, 7)]),  # флешка -- другой номер
        ({**_EV_DISK1, "disk": None}, [_arr("2026-08-16T12:00:00.0000000Z", 1, 7)]),
        ({**_EV_DISK1, "source": "Ntfs"}, [_arr("2026-08-16T12:00:00.0000000Z", 1, 7)]),
    ],
)
def test_events_unproven_removable_disk_keeps_source(monkeypatch, event, arrivals):
    """Исключаем только положительно опознанное съёмное; иначе -- как раньше (считается)."""
    out = _events_with(monkeypatch, [event], arrivals)
    assert out[0]["source"] == event["source"]


def test_events_reused_disk_number_uses_latest_arrival_before_event(monkeypatch):
    """Номера дисков переиспользуются: решает последнее подключение не позже события."""
    early = {**_EV_DISK1, "ts": "2026-08-16T10:00:00.0000000Z"}
    late = {**_EV_DISK1, "ts": "2026-08-16T14:00:00.0000000Z"}
    arrivals = [
        _arr("2026-08-16T09:00:00.0000000Z", 1, 7),  # диск 1 -- флешка
        _arr("2026-08-16T13:00:00.0000000Z", 1, 11),  # потом диск 1 -- SATA
    ]
    out = _events_with(monkeypatch, [early, late], arrivals)
    assert [e["source"] for e in out] == ["disk-removable", "disk"]


def test_heartbeat_disk_latency_reads_system_disk_not_total() -> None:
    """USB: в _Total входит копирование на флешку -- берём экземпляр диска системы («0 C:»).

    Не нашли (счётчики выключены, необычная разметка) -- _Total, как раньше.
    """
    s = heartbeat._SCRIPT
    assert "PhysicalDisk -Filter \"Name='_Total'\"" not in s
    assert s.count("PhysicalDisk -Filter \"Name='$sysDisk'\"") == 3  # formatted + 2 raw
    assert "-contains $env:SystemDrive" in s
    assert "$sysDisk = '_Total'" in s
