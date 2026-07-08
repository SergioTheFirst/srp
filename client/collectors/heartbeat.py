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
from client.collectors.sources import (
    DISK_LATENCY,
    FREE_SPACE,
    THROTTLE,
    CollectorResult,
    failed,
    field_status,
    health,
)

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

# ssd3 Ф4 (T4.1, K6 -- passive): 8 raw-counter samples ~2s apart -> 7 deltas of
# PERF_AVERAGE_TIMER latency (ms). Raw class (not the Formatted one above) so we
# can build the ratio ourselves; ΔBase=0 (no read/write activity that slice) is
# skipped per-channel rather than fabricating a latency of zero. Whole block is
# best-effort: a failure here must not cost the rest of this heartbeat (K5).
function Get-Pctl($sorted, $p) {
  $n = $sorted.Count
  if ($n -eq 0) { return $null }
  $idx = [math]::Ceiling($p * $n) - 1
  if ($idx -lt 0) { $idx = 0 }
  if ($idx -ge $n) { $idx = $n - 1 }
  return $sorted[$idx]
}

$rReadings = @(); $wReadings = @()
try {
  $prev = Get-CimInstance Win32_PerfRawData_PerfDisk_PhysicalDisk -Filter "Name='_Total'"
  $freq = [double]$prev.Frequency_PerfTime
  for ($i = 0; $i -lt 7; $i++) {
    Start-Sleep -Milliseconds 2000
    $cur = Get-CimInstance Win32_PerfRawData_PerfDisk_PhysicalDisk -Filter "Name='_Total'"
    if ($freq -gt 0) {
      $dbR = [double]$cur.AvgDisksecPerRead_Base - [double]$prev.AvgDisksecPerRead_Base
      if ($dbR -gt 0) {
        $rReadings += (([double]$cur.AvgDisksecPerRead - [double]$prev.AvgDisksecPerRead) / $freq) / $dbR * 1000
      }
      $dbW = [double]$cur.AvgDisksecPerWrite_Base - [double]$prev.AvgDisksecPerWrite_Base
      if ($dbW -gt 0) {
        $wReadings += (([double]$cur.AvgDisksecPerWrite - [double]$prev.AvgDisksecPerWrite) / $freq) / $dbW * 1000
      }
    }
    $prev = $cur
  }
} catch { $rReadings = @(); $wReadings = @() }

$rSorted = @($rReadings | Sort-Object)
$wSorted = @($wReadings | Sort-Object)
$allSorted = @(($rReadings + $wReadings) | Sort-Object)
$rP50 = if ($rSorted.Count -ge 4) { [math]::Round((Get-Pctl $rSorted 0.50), 3) } else { $null }
$rP95 = if ($rSorted.Count -ge 4) { [math]::Round((Get-Pctl $rSorted 0.95), 3) } else { $null }
$wP50 = if ($wSorted.Count -ge 4) { [math]::Round((Get-Pctl $wSorted 0.50), 3) } else { $null }
$wP95 = if ($wSorted.Count -ge 4) { [math]::Round((Get-Pctl $wSorted 0.95), 3) } else { $null }
$latMax = if ($allSorted.Count -gt 0) { [math]::Round($allSorted[$allSorted.Count - 1], 3) } else { $null }

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
  disk_read_ms_p50   = $rP50
  disk_read_ms_p95   = $rP95
  disk_write_ms_p50  = $wP50
  disk_write_ms_p95  = $wP95
  disk_lat_max_ms    = $latMax
  disk_lat_samples   = $rSorted.Count
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


def collect_heartbeat() -> CollectorResult:
    # ssd3 Ф4: script budget grew to ~14s (7 x 2s sleeps) for the tail-latency
    # micro-series; timeout raised from 45 with headroom (T4.1: budget <= 30s).
    result = run_ps(_SCRIPT, timeout=75)
    owned = [FREE_SPACE, THROTTLE, DISK_LATENCY]
    if result.status != "ok" or not isinstance(result.data, dict):
        status = result.status if result.status != "ok" else "partial"
        return CollectorResult(None, failed(owned, status))
    raw = result.data
    payload = {
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
        "disk_read_ms_p50": _f(raw.get("disk_read_ms_p50")),
        "disk_read_ms_p95": _f(raw.get("disk_read_ms_p95")),
        "disk_write_ms_p50": _f(raw.get("disk_write_ms_p50")),
        "disk_write_ms_p95": _f(raw.get("disk_write_ms_p95")),
        "disk_lat_max_ms": _f(raw.get("disk_lat_max_ms")),
        "disk_lat_samples": _i(raw.get("disk_lat_samples")),
    }
    sh = {
        FREE_SPACE: health(field_status(payload.get("free_space_pct") is not None)),
        THROTTLE: health(field_status(payload.get("cpu_perf_pct") is not None)),
        DISK_LATENCY: health(
            field_status(
                payload.get("disk_read_sec") is not None
                or payload.get("disk_write_sec") is not None
            )
        ),
    }
    return CollectorResult(payload, sh)
