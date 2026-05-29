r"""Heartbeat: live vitals sampled cheaply, language-neutral.

We read ``Win32_PerfFormattedData_*`` CIM classes rather than ``Get-Counter``:
counter *path* names are localized (a Russian Windows has no
``\Processor(_Total)\% Processor Time``), so English paths fail outright. The
CIM perf classes use stable English property names on every locale.

Known MVP limitation: ``AvgDisksecPerRead/Write`` in the formatted class is an
integer, so sub-second disk latency truncates to 0 (a healthy disk reads as
0 -> no false alarm; only multi-second stalls surface). ``cpu_perf_pct`` (%
Processor Performance) is a throttle proxy and is naturally low at idle due to
SpeedStep -- it is only meaningful under load.
"""

from __future__ import annotations

from typing import Any, Optional

from client.collectors.ps import run_ps

_SCRIPT = r"""
$cpu  = Get-CimInstance Win32_PerfFormattedData_PerfOS_Processor -Filter "Name='_Total'"
$pi   = Get-CimInstance Win32_PerfFormattedData_Counters_ProcessorInformation -Filter "Name='_Total'" | Select-Object -First 1
$mem  = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
$pf   = Get-CimInstance Win32_PerfFormattedData_PerfOS_PagingFile -Filter "Name='_Total'"
$dsk  = Get-CimInstance Win32_PerfFormattedData_PerfDisk_PhysicalDisk -Filter "Name='_Total'"
$proc = Get-CimInstance Win32_PerfFormattedData_PerfProc_Process -Filter "Name='_Total'"
$os   = Get-CimInstance Win32_OperatingSystem
$ld   = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$($env:SystemDrive)'"

$nicErr = 0
foreach ($n in Get-CimInstance Win32_PerfFormattedData_Tcpip_NetworkInterface) {
  $nicErr += [int]$n.PacketsReceivedErrors + [int]$n.PacketsOutboundErrors
}
$explorer = @(Get-Process explorer -ErrorAction SilentlyContinue).Count -gt 0
$uptimeH  = if ($os.LastBootUpTime) { [math]::Round(((Get-Date) - $os.LastBootUpTime).TotalHours, 1) } else { $null }
$freePct  = if ($ld -and $ld.Size -gt 0) { [math]::Round(($ld.FreeSpace / $ld.Size) * 100, 1) } else { $null }

[ordered]@{
  cpu_pct            = $cpu.PercentProcessorTime
  cpu_perf_pct       = if ($pi) { $pi.PercentProcessorPerformance } else { $null }
  mem_avail_mb       = $mem.AvailableMBytes
  committed_pct      = $mem.PercentCommittedBytesInUse
  pagefile_pct       = $pf.PercentUsage
  disk_read_sec      = $dsk.AvgDisksecPerRead
  disk_write_sec     = $dsk.AvgDisksecPerWrite
  disk_queue         = $dsk.CurrentDiskQueueLength
  free_space_pct     = $freePct
  handle_count_total = $proc.HandleCount
  nic_errors         = $nicErr
  user_present       = $explorer
  uptime_hours       = $uptimeH
} | ConvertTo-Json -Compress
"""


def _f(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> Optional[int]:
    f = _f(v)
    return None if f is None else int(f)


def collect_heartbeat() -> Optional[dict[str, Any]]:
    raw = run_ps(_SCRIPT, timeout=45)
    if not isinstance(raw, dict):
        return None
    return {
        "cpu_pct": _f(raw.get("cpu_pct")),
        "cpu_perf_pct": _f(raw.get("cpu_perf_pct")),
        "mem_avail_mb": _f(raw.get("mem_avail_mb")),
        "committed_pct": _f(raw.get("committed_pct")),
        "pagefile_pct": _f(raw.get("pagefile_pct")),
        "disk_read_sec": _f(raw.get("disk_read_sec")),
        "disk_write_sec": _f(raw.get("disk_write_sec")),
        "disk_queue": _f(raw.get("disk_queue")),
        "free_space_pct": _f(raw.get("free_space_pct")),
        "handle_count_total": _i(raw.get("handle_count_total")),
        "nic_errors": _i(raw.get("nic_errors")),
        "user_present": bool(raw.get("user_present")),
        "uptime_hours": _f(raw.get("uptime_hours")),
    }
