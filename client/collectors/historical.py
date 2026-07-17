"""Historical: the day-1 scan -- the machine's own past is a free dataset.

Pulls 30-day failure-event counts (Get-WinEvent), the latest Reliability
Monitor stability index, and SMART-ish storage counters
(Get-StorageReliabilityCounter). Every source is wrapped so a disabled log
or missing counter yields a neutral gap, not a crash.
"""

from __future__ import annotations

from client.collectors.inventory import hash_serial
from client.collectors.network import collect_network
from client.collectors.printer_ports import collect_printer_ports
from client.collectors.ps import as_list, run_ps
from client.collectors.smart import collect_smart
from client.collectors.sources import (
    BOOT_TIME,
    CERTIFICATES,
    NETWORK,
    RELIABILITY,
    SMART,
    STORAGE_RELIABILITY,
    CollectorResult,
    failed,
    field_status,
    health,
)
from client.collectors.user_certs import collect_user_certs

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
    try {
      $rc = $pd | Get-StorageReliabilityCounter -ErrorAction SilentlyContinue
      if ($rc) {
        $storage += [ordered]@{
          disk="$($pd.FriendlyName)".Trim(); media_type="$($pd.MediaType)";
          wear_pct=$rc.Wear; power_on_hours=$rc.PowerOnHours;
          read_errors_total=$rc.ReadErrorsTotal; write_errors_total=$rc.WriteErrorsTotal;
          temperature_c=$rc.Temperature;
          serial="$($pd.SerialNumber)".Trim(); bus_type="$($pd.BusType)";
          read_errors_uncorrected=$rc.ReadErrorsUncorrected; write_errors_uncorrected=$rc.WriteErrorsUncorrected;
          start_stop_cycles=$rc.StartStopCycleCount; load_unload_cycles=$rc.LoadUnloadCycleCount;
          flush_latency_max_ms=$rc.FlushLatencyMax }
      }
    } catch {}
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
  observation_days    = 30
} | ConvertTo-Json -Depth 5 -Compress
"""

_CERT_SCRIPT = r"""
$certs = @()
foreach ($store in 'Cert:\LocalMachine\My','Cert:\CurrentUser\My') {
  try {
    foreach ($c in Get-ChildItem $store -ErrorAction SilentlyContinue) {
      $certs += [ordered]@{
        subject="$($c.Subject)"; issuer="$($c.Issuer)"; thumbprint="$($c.Thumbprint)";
        not_after=$c.NotAfter.ToUniversalTime().ToString('o');
        not_before=$c.NotBefore.ToUniversalTime().ToString('o') }
    }
  } catch {}
}
@{ certificates = @($certs) } | ConvertTo-Json -Depth 4 -Compress
"""


def collect_historical(active_scan: bool = False) -> CollectorResult:
    result = run_ps(_SCRIPT, timeout=120)
    owned = [STORAGE_RELIABILITY, RELIABILITY, BOOT_TIME, CERTIFICATES, NETWORK]
    if result.status != "ok" or not isinstance(result.data, dict):
        status = result.status if result.status != "ok" else "partial"
        return CollectorResult(None, failed(owned, status))
    raw = result.data
    raw["storage"] = as_list(raw.get("storage"))
    for row in raw["storage"]:
        if isinstance(row, dict):
            # Raw serial hashed immediately -- it must never leave the agent
            # (see hash_serial; the hash is the disk_key, not the serial).
            row["serial_hash"] = hash_serial(row.pop("serial", None))

    rsi = raw.get("reliability_stability_index")
    counts_present = any(
        raw.get(k) is not None
        for k in (
            "kernel_power_41_30d",
            "dirty_shutdowns_30d",
            "bugchecks_30d",
            "app_crashes_30d",
            "whea_errors_30d",
        )
    )
    rel_status = "ok" if rsi is not None else ("partial" if counts_present else "empty")
    sh = {
        STORAGE_RELIABILITY: health(field_status(bool(raw["storage"]))),
        RELIABILITY: health(rel_status),
        BOOT_TIME: health(field_status(raw.get("avg_boot_ms") is not None)),
    }

    # Deep SMART (Tier A ATA via CIM + Tier B NVMe via IOCTL): its own script,
    # its own error domain, layered onto the base storage rows by serial_hash.
    merged_storage, smart_status = collect_smart(raw["storage"])
    raw["storage"] = merged_storage
    sh[SMART] = health(smart_status)

    # Certificate metadata: separate script, separate error domain.
    cert_res = run_ps(_CERT_SCRIPT, timeout=60)
    if cert_res.status == "ok" and isinstance(cert_res.data, dict):
        certs = [
            {
                "subject": c.get("subject"),
                "issuer": c.get("issuer"),
                "thumbprint": c.get("thumbprint"),
                "not_after": c.get("not_after"),
                "not_before": c.get("not_before"),
            }
            for c in as_list(cert_res.data.get("certificates"))
            if isinstance(c, dict)
        ]
        raw["certificates"] = certs
        sh[CERTIFICATES] = health(field_status(bool(certs)))
    else:
        raw["certificates"] = []
        err_status = cert_res.status if cert_res.status != "ok" else "empty"
        sh[CERTIFICATES] = health(err_status)

    # Personal certs spooled by per-user trays (stage 8): the SYSTEM agent can't see
    # CurrentUser\My, so the tray drops metadata into C:\SRP\spool. Untrusted, user-
    # writable input -> strictly validated in user_certs; informational, not a trust domain.
    raw["user_certificates"] = collect_user_certs()

    # Network metadata: separate script, separate error domain (certificates-style).
    net = collect_network(active_scan=active_scan)
    if net.payload is not None:
        raw.update(net.payload)
    else:
        raw["network_adapters"] = []
        raw["network_neighbors"] = []
        raw["network_connections"] = []
        raw["network_quality"] = []
        raw["network_routes"] = []
    sh.update(net.source_health)

    # Printer-port discovery hints: reads local spooler config (silent, not a scan).
    # Informational only -> no source_health, never a trust domain (spec §12).
    ports = collect_printer_ports()
    raw["printer_ports"] = ports.payload["printer_ports"] if ports.payload else []

    return CollectorResult(raw, sh)
