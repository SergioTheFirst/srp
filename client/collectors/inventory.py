"""Inventory: slow-changing machine identity (CIM/WMI via PowerShell).

Disk serials are hashed here and the raw value is dropped before the payload
leaves the process -- the server only ever sees ``serial_hash``.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Optional

from client.collectors.ps import run_ps
from client.collectors.sources import IDENTITY, CollectorResult, failed, health

_GIB = 1024**3

# SMBIOS chassis type codes -> coarse class.
_LAPTOP = {8, 9, 10, 11, 12, 14, 18, 21, 30, 31, 32}
_DESKTOP = {3, 4, 5, 6, 7, 13, 15, 16, 17, 23, 24}

_SCRIPT = r"""
$cs   = Get-CimInstance Win32_ComputerSystem
$os   = Get-CimInstance Win32_OperatingSystem
$bios = Get-CimInstance Win32_BIOS
$cpu  = Get-CimInstance Win32_Processor | Select-Object -First 1
$enc  = Get-CimInstance Win32_SystemEnclosure | Select-Object -First 1

$mods = @(Get-CimInstance Win32_PhysicalMemory | ForEach-Object {
  [ordered]@{ capacity_bytes=[int64]$_.Capacity; speed_mhz=$_.Speed;
              manufacturer="$($_.Manufacturer)".Trim(); part_number="$($_.PartNumber)".Trim() } })

$disks = @()
try {
  $disks = @(Get-PhysicalDisk | ForEach-Object {
    [ordered]@{ model="$($_.FriendlyName)".Trim(); media_type="$($_.MediaType)";
                size_bytes=[int64]$_.Size; serial="$($_.SerialNumber)".Trim();
                bus_type="$($_.BusType)"; firmware="$($_.FirmwareVersion)" } }) } catch {}
if (-not $disks) {
  $disks = @(Get-CimInstance Win32_DiskDrive | ForEach-Object {
    [ordered]@{ model="$($_.Model)".Trim(); media_type=$null; size_bytes=[int64]$_.Size;
                serial="$($_.SerialNumber)".Trim(); interface="$($_.InterfaceType)";
                firmware="$($_.FirmwareRevision)" } }) }

# Проблемные драйверы: без кода 22 (отключено вручную) и без периферии ПО ПРИРОДЕ: очереди печати/
# сканирования, сопряжённые BT-устройства, узлы флешек. Флаг InLocalMachineContainer НЕ годится:
# на ноутбуках он $false и у встроенных USB-камеры/BT-модуля/картридера (проверено живьём). Сбой
# перечисления -> null («неизвестно»), не «0 проблем».
$drv = $null
$periph = '^(SWD\\(PRINTENUM|IPP|ESCL|DAFWSDPROVIDER|WPDBUSENUM)|BTHENUM|BTHLE|BTHLEDEVICE|USBSTOR)\\'
try {
  $probs = @(Get-CimInstance Win32_PnPEntity -Filter "ConfigManagerErrorCode <> 0 AND ConfigManagerErrorCode <> 22" -ErrorAction Stop)
  $drv = @($probs | Where-Object { "$($_.PNPDeviceID)" -notmatch $periph }).Count
} catch {}

# Очередь переименований Session Manager (PFRO) не смотрим: хвосты EdgeUpdate там почти на каждом
# ПК, а Fast Startup её не разбирает -- флаг висел бы до рестарта (ложные «зависшие обновления»).
$pending = $false
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') { $pending = $true }
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') { $pending = $true }

# Агент 0.3.8: гигиена безопасности. Каждый блок в своём try -- сбой = null («неизвестно»).
# Только числа/DateTime/английские значения; локализованный текст не разбираем.
$patch = $null
try {
  $last = @(Get-CimInstance Win32_QuickFixEngineering -ErrorAction Stop |
    ForEach-Object { $_.InstalledOn } | Where-Object { $_ -is [datetime] }) |
    Sort-Object -Descending | Select-Object -First 1
  if ($last) { $patch = $last.ToString('yyyy-MM-dd', [Globalization.CultureInfo]::InvariantCulture) }
} catch {}

$av = $null
try {
  $av = @(Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct -ErrorAction Stop |
    ForEach-Object { [ordered]@{ name="$($_.displayName)".Trim(); state=[int64]$_.productState } })
} catch {}

$bl = $null
try {
  $vol = Get-CimInstance -Namespace root/cimv2/Security/MicrosoftVolumeEncryption -ClassName Win32_EncryptableVolume -Filter "DriveLetter='$env:SystemDrive'" -ErrorAction Stop | Select-Object -First 1
  if ($vol) { $bl = [int]$vol.ProtectionStatus }
} catch {}

$tpmPresent = $null; $tpmSpec = $null
try {
  $tpm = @(Get-CimInstance -Namespace root/cimv2/Security/MicrosoftTpm -ClassName Win32_Tpm -ErrorAction Stop)
  $tpmPresent = $tpm.Count -gt 0
  if ($tpmPresent) { $tpmSpec = "$($tpm[0].SpecVersion)" }
} catch {}

$sb = $null
try { $sb = [int](Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\SecureBoot\State' -ErrorAction Stop).UEFISecureBootEnabled } catch {}

$lic = $null
try {
  $lic = @(Get-CimInstance SoftwareLicensingProduct -Filter "ApplicationID='55c92734-d682-4d71-983e-d6ec3f16059f' AND PartialProductKey IS NOT NULL" -ErrorAction Stop |
    ForEach-Object { [int]$_.LicenseStatus })
} catch {}

[ordered]@{
  hostname        = $env:COMPUTERNAME
  manufacturer    = "$($cs.Manufacturer)".Trim()
  model           = "$($cs.Model)".Trim()
  chassis_types   = @($enc.ChassisTypes)
  os_caption      = "$($os.Caption)".Trim()
  os_version      = $os.Version
  os_build        = $os.BuildNumber
  os_install_date = if ($os.InstallDate) { $os.InstallDate.ToString('yyyy-MM-dd') } else { $null }
  bios_version    = "$($bios.SMBIOSBIOSVersion)".Trim()
  bios_release_date = if ($bios.ReleaseDate) { $bios.ReleaseDate.ToString('yyyy-MM-dd') } else { $null }
  cpu_name        = "$($cpu.Name)".Trim()
  cpu_cores       = $cpu.NumberOfCores
  cpu_logical     = $cpu.NumberOfLogicalProcessors
  total_ram_bytes = [int64]$cs.TotalPhysicalMemory
  memory_modules  = $mods
  disks           = $disks
  driver_problem_count = $drv
  pending_reboot  = $pending
  last_patch_date = $patch
  antivirus       = $av
  bitlocker_status = $bl
  tpm_present     = $tpmPresent
  tpm_spec        = $tpmSpec
  secure_boot     = $sb
  license_status  = $lic
} | ConvertTo-Json -Depth 5 -Compress
"""


def _hash_serial(serial: Optional[str]) -> Optional[str]:
    s = (serial or "").strip()
    if not s or s.lower() in ("", "none", "to be filled by o.e.m.", "0"):
        return None
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()[:16]


# Public alias: client/collectors/smart.py hashes raw serials the same way so
# the disk_key used by the storage/SMART engine matches DiskInfo.serial_hash.
hash_serial = _hash_serial


def _chassis(codes: Any) -> str:
    for c in codes or []:
        try:
            n = int(c)
        except (TypeError, ValueError):
            continue
        if n in _LAPTOP:
            return "laptop"
        if n in _DESKTOP:
            return "desktop"
    return "unknown"


def _gb(num_bytes: Any) -> Optional[float]:
    try:
        b = float(num_bytes)
    except (TypeError, ValueError):
        return None
    return round(b / _GIB, 1) if b > 0 else None


MAX_AV = 8  # = schema InventoryPayload.antivirus max_length
MAX_AV_NAME = 128  # = schema AntivirusInfo.name max_length
# productState: биты 12-15 = состояние (1 = вкл; 0 = выкл, 2 = отложен, 3 = истёк --
# 0x062100 у реально выключенного Defender проверено живьём), биты 4-7 = базы (0 = свежие).
_AV_ON = 1
_BITLOCKER = {0: "off", 1: "on"}  # ProtectionStatus; 2 и прочее -> unknown
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _antivirus(raw: Any) -> Optional[list[dict[str, Any]]]:
    if not isinstance(raw, list):
        return None
    out = []
    for item in raw[:MAX_AV]:
        if not isinstance(item, dict):
            continue
        raw_state = item.get("state")
        state = (
            raw_state if isinstance(raw_state, int) and not isinstance(raw_state, bool) else None
        )
        out.append(
            {
                "name": (str(item.get("name") or "").strip()[:MAX_AV_NAME] or None),
                "enabled": None if state is None else (state >> 12) & 0xF == _AV_ON,
                "up_to_date": None if state is None else (state >> 4) & 0xF == 0,
            }
        )
    return out


def _security(raw: dict[str, Any]) -> dict[str, Any]:
    """Гигиена безопасности (0.3.8): мусор/сбой -> None («неизвестно»)."""
    patch = raw.get("last_patch_date")
    bl = raw.get("bitlocker_status")
    tpm = raw.get("tpm_present")
    spec = str(raw.get("tpm_spec") or "").split(",")[0].strip()
    sb = raw.get("secure_boot")
    lic = raw.get("license_status")
    return {
        "last_patch_date": patch if isinstance(patch, str) and _DATE_RE.fullmatch(patch) else None,
        "antivirus": _antivirus(raw.get("antivirus")),
        "bitlocker_system": None if bl is None else _BITLOCKER.get(bl, "unknown"),
        "tpm_present": tpm if isinstance(tpm, bool) else None,
        "tpm_version": (spec[:16] or None) if tpm is True else None,
        "secure_boot": bool(sb) if sb in (0, 1) and not isinstance(sb, bool) else None,
        # Пустой ответ = ключ не найден, это не подтверждённое «не активирована» -> None.
        "windows_activated": (1 in lic) if isinstance(lic, list) and lic else None,
    }


def collect_inventory() -> CollectorResult:
    result = run_ps(_SCRIPT, timeout=60)
    if result.status != "ok" or not isinstance(result.data, dict):
        status = result.status if result.status != "ok" else "partial"
        return CollectorResult(None, failed([IDENTITY], status, result.reason))
    raw = result.data

    disks = []
    for d in raw.get("disks") or []:
        disks.append(
            {
                "model": d.get("model") or None,
                "media_type": d.get("media_type") or None,
                "size_gb": _gb(d.get("size_bytes")),
                "serial_hash": _hash_serial(d.get("serial")),
                "firmware": d.get("firmware") or None,
                "interface": d.get("interface") or None,
                "bus_type": d.get("bus_type") or None,
            }
        )

    modules = []
    for m in raw.get("memory_modules") or []:
        modules.append(
            {
                "capacity_gb": _gb(m.get("capacity_bytes")),
                "speed_mhz": m.get("speed_mhz"),
                "manufacturer": (m.get("manufacturer") or None),
                "part_number": (m.get("part_number") or None),
            }
        )

    payload = {
        "hostname": raw.get("hostname"),
        "manufacturer": raw.get("manufacturer") or None,
        "model": raw.get("model") or None,
        "chassis": _chassis(raw.get("chassis_types")),
        "os_caption": raw.get("os_caption") or None,
        "os_version": raw.get("os_version") or None,
        "os_build": raw.get("os_build") or None,
        "os_install_date": raw.get("os_install_date"),
        "bios_version": raw.get("bios_version") or None,
        "bios_release_date": raw.get("bios_release_date"),
        "cpu_name": raw.get("cpu_name") or None,
        "cpu_cores": raw.get("cpu_cores"),
        "cpu_logical": raw.get("cpu_logical"),
        "total_ram_gb": _gb(raw.get("total_ram_bytes")),
        "memory_modules": modules,
        "disks": disks,
        "driver_problem_count": raw.get("driver_problem_count"),
        "pending_reboot": bool(raw.get("pending_reboot")),
        **_security(raw),
    }
    # hostname is $env:COMPUTERNAME -- present even when every CIM query failed
    # (broken WMI repository, winmgmt stopping). Without model AND OS the run is
    # hollow: report identity "empty" and send no payload, so the server keeps the
    # last good inventory instead of a blank one (F2) and the 0.3.2 retry fires.
    if not (payload["model"] or payload["os_caption"]):
        return CollectorResult(None, failed([IDENTITY], "empty", "cim_hollow"))
    return CollectorResult(payload, {IDENTITY: health("ok")})
