"""Inventory: slow-changing machine identity (CIM/WMI via PowerShell).

Disk serials are hashed here and the raw value is dropped before the payload
leaves the process -- the server only ever sees ``serial_hash``.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional

from client.collectors.ps import run_ps
from client.collectors.sources import IDENTITY, CollectorResult, failed, field_status, health

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

$drv = @(Get-CimInstance Win32_PnPEntity -Filter "ConfigManagerErrorCode <> 0").Count

$pending = $false
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending') { $pending = $true }
if (Test-Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired') { $pending = $true }
$pfro = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' -Name PendingFileRenameOperations -ErrorAction SilentlyContinue).PendingFileRenameOperations
if ($pfro) { $pending = $true }

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
} | ConvertTo-Json -Depth 5 -Compress
"""


def _hash_serial(serial: Optional[str]) -> Optional[str]:
    s = (serial or "").strip()
    if not s or s.lower() in ("", "none", "to be filled by o.e.m.", "0"):
        return None
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()[:16]


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


def collect_inventory() -> CollectorResult:
    result = run_ps(_SCRIPT, timeout=60)
    if result.status != "ok" or not isinstance(result.data, dict):
        status = result.status if result.status != "ok" else "partial"
        return CollectorResult(None, failed([IDENTITY], status))
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
    }
    present = bool(payload.get("hostname") or payload.get("model"))
    return CollectorResult(payload, {IDENTITY: health(field_status(present))})
