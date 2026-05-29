"""Historical: the day-1 scan -- the machine's own past is a free dataset.

Pulls 30-day failure-event counts (Get-WinEvent), the latest Reliability
Monitor stability index, SMART-ish storage counters (Get-StorageReliabilityCounter),
and battery design-vs-full capacity. Every source is wrapped so a disabled log
or missing counter yields a neutral gap, not a crash.
"""

from __future__ import annotations

from typing import Any, Optional

from client.collectors.ps import as_list, run_ps

_SCRIPT = r"""
$start = (Get-Date).AddDays(-30)

function CountEv($ht) { try { @(Get-WinEvent -FilterHashtable $ht -ErrorAction SilentlyContinue).Count } catch { 0 } }

$kp   = CountEv @{LogName='System'; ProviderName='Microsoft-Windows-Kernel-Power'; Id=41; StartTime=$start}
$ds   = CountEv @{LogName='System'; Id=6008; StartTime=$start}
$bc   = CountEv @{LogName='System'; ProviderName='Microsoft-Windows-WER-SystemErrorReporting'; Id=1001; StartTime=$start}
$ac   = CountEv @{LogName='Application'; ProviderName='Application Error'; Id=1000; StartTime=$start}
$whea = CountEv @{LogName='System'; ProviderName='Microsoft-Windows-WHEA-Logger'; StartTime=$start}

$bt = @()
try {
  foreach ($e in Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-Diagnostics-Performance/Operational'; Id=100; StartTime=$start} -MaxEvents 20 -ErrorAction SilentlyContinue) {
    $x = [xml]$e.ToXml()
    $v = ($x.Event.EventData.Data | Where-Object { $_.Name -eq 'BootTime' }).'#text'
    if ($v) { $bt += [double]$v }
  }
} catch {}
$avgBoot = if ($bt.Count) { [int][math]::Round(($bt | Measure-Object -Average).Average) } else { $null }

$rsi = $null
try {
  $m = Get-CimInstance -ClassName Win32_ReliabilityStabilityMetrics -ErrorAction SilentlyContinue |
       Sort-Object TimeGenerated -Descending | Select-Object -First 1
  if ($m) { $rsi = [math]::Round([double]$m.SystemStabilityIndex, 1) }
} catch {}

$storage = @()
try {
  foreach ($pd in Get-PhysicalDisk -ErrorAction SilentlyContinue) {
    $rc = $pd | Get-StorageReliabilityCounter -ErrorAction SilentlyContinue
    if ($rc) {
      $storage += [ordered]@{
        disk="$($pd.FriendlyName)".Trim(); media_type="$($pd.MediaType)";
        wear_pct=$rc.Wear; power_on_hours=$rc.PowerOnHours;
        read_errors_total=$rc.ReadErrorsTotal; write_errors_total=$rc.WriteErrorsTotal;
        temperature_c=$rc.Temperature }
    }
  }
} catch {}

$battery = [ordered]@{ present=$false }
try {
  $bs  = Get-CimInstance -Namespace root\wmi -ClassName BatteryStaticData -ErrorAction SilentlyContinue | Select-Object -First 1
  $bf  = Get-CimInstance -Namespace root\wmi -ClassName BatteryFullChargedCapacity -ErrorAction SilentlyContinue | Select-Object -First 1
  $bcc = Get-CimInstance -Namespace root\wmi -ClassName BatteryCycleCount -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($bs) {
    $design = [double]$bs.DesignedCapacity
    $full   = if ($bf) { [double]$bf.FullChargedCapacity } else { $null }
    $wear   = if ($design -gt 0 -and $full) { [math]::Round((1 - ($full / $design)) * 100, 1) } else { $null }
    $battery = [ordered]@{
      present=$true; design_capacity_mwh=[int]$design;
      full_charge_capacity_mwh= if ($full) { [int]$full } else { $null };
      wear_pct=$wear; cycle_count= if ($bcc) { [int]$bcc.CycleCount } else { $null } }
  }
} catch {}

[ordered]@{
  reliability_stability_index = $rsi
  kernel_power_41_30d = $kp
  dirty_shutdowns_30d = $ds
  bugchecks_30d       = $bc
  app_crashes_30d     = $ac
  whea_errors_30d     = $whea
  avg_boot_ms         = $avgBoot
  storage             = $storage
  battery             = $battery
  observation_days    = 30
} | ConvertTo-Json -Depth 5 -Compress
"""


def collect_historical() -> Optional[dict[str, Any]]:
    raw = run_ps(_SCRIPT, timeout=120)
    if not isinstance(raw, dict):
        return None
    raw["storage"] = as_list(raw.get("storage"))
    bat = raw.get("battery")
    if not isinstance(bat, dict):
        raw["battery"] = {"present": False}
    return raw
