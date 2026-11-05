"""Historical: the day-1 scan -- the machine's own past is a free dataset.

Pulls 30-day failure-event counts (Get-WinEvent), the latest Reliability
Monitor stability index, and SMART-ish storage counters
(Get-StorageReliabilityCounter). Every source is wrapped so a disabled log
or missing counter yields a neutral gap, not a crash.
"""

from __future__ import annotations

import re
from typing import Any, Optional

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

$bt = @(); $btNormal = @(); $lastBoot = $null
# Перечисление журнала -- под своей защитой (журнал может отсутствовать на Server SKU),
# иначе падение здесь уносит ВЕСЬ конверт historical (5 доменов в одном JSON).
$btEvents = @()
try { $btEvents = @(Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-Diagnostics-Performance/Operational'; Id=100; StartTime=$start} -MaxEvents 20 -ErrorAction SilentlyContinue) } catch { $btEvents = @() }
foreach ($e in $btEvents) {
  try {
    $d = ([xml]$e.ToXml()).Event.EventData.Data
    $v = ($d | Where-Object { $_.Name -eq 'BootTime' }).'#text'
    if ($v) {
      $bt += [double]$v
      # xs:boolean рендерится 'true'/'false' на любом языке Windows.
      if (($d | Where-Object { $_.Name -eq 'BootIsRebootAfterInstall' }).'#text' -eq 'false') {
        $btNormal += [double]$v
        # agent 1.0.0 (H17): Get-WinEvent returns newest first -- the first normal boot
        if ($null -eq $lastBoot) { $lastBoot = [int][double]$v }
      }
    }
  } catch { continue }
}
# Медиана обычных загрузок: одна долгая перезагрузка после обновлений не тянет её вверх. Нет
# обычных -- медиана всех (boot_time -- источник доверия, не должен уходить в EMPTY).
$use = $bt; if ($btNormal.Count) { $use = $btNormal }
$s = @($use | Sort-Object); $n = $s.Count
$avgBoot = if ($n) { [int][math]::Round(($s[[math]::Floor(($n - 1) / 2)] + $s[[math]::Floor($n / 2)]) / 2) } else { $null }

# agent 1.0.0 (H17): the BugCheck 1001 and Kernel-Power 41 records of 30 days, newest
# first, at most 20 each. Properties[0] of a 1001 is the hex bug check line, [1] the dump
# path (only its kind leaves the agent -- the parent maps it); KP41 by EventData names.
$bugchecks = @(); $bcEvents = @()
try { $bcEvents = @(Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-WER-SystemErrorReporting'; Id=1001; StartTime=$start} -MaxEvents 20 -ErrorAction SilentlyContinue) } catch { $bcEvents = @() }
foreach ($e in $bcEvents) {
  try {
    $bugchecks += [ordered]@{ ts = $e.TimeCreated.ToUniversalTime().ToString('o');
      raw = "$($e.Properties[0].Value)"; dumpraw = "$($e.Properties[1].Value)" }
  } catch { continue }
}
$kp41 = @(); $kpEvents = @()
try { $kpEvents = @(Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-Kernel-Power'; Id=41; StartTime=$start} -MaxEvents 20 -ErrorAction SilentlyContinue) } catch { $kpEvents = @() }
foreach ($e in $kpEvents) {
  try {
    $d = ([xml]$e.ToXml()).Event.EventData.Data
    $kp41 += [ordered]@{ ts = $e.TimeCreated.ToUniversalTime().ToString('o');
      code = ($d | Where-Object { $_.Name -eq 'BugcheckCode' }).'#text';
      pb = ($d | Where-Object { $_.Name -eq 'PowerButtonTimestamp' }).'#text' }
  } catch { continue }
}

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
  last_boot_ms        = $lastBoot
  bugchecks           = @($bugchecks)
  kp41                = @($kp41)
  storage             = $storage
  observation_days    = 30
} | ConvertTo-Json -Depth 5 -Compress
"""

_CERT_SCRIPT = r"""
$certs = @()
foreach ($store in 'Cert:\LocalMachine\My','Cert:\CurrentUser\My') {
  # Перечисление хранилища -- под своей защитой: битое хранилище/CSP не должно
  # уносить сертификаты, уже собранные из другого хранилища.
  $items = @()
  try { $items = @(Get-ChildItem $store -ErrorAction SilentlyContinue) } catch { continue }
  foreach ($c in $items) {
    try {
      $certs += [ordered]@{
        subject="$($c.Subject)"; issuer="$($c.Issuer)"; thumbprint="$($c.Thumbprint)";
        not_after=$c.NotAfter.ToUniversalTime().ToString('o');
        not_before=$c.NotBefore.ToUniversalTime().ToString('o') }
    } catch { continue }
  }
}
@{ certificates = @($certs) } | ConvertTo-Json -Depth 4 -Compress
"""


# agent 1.0.0 (H17): the caps of shared.schema (BUGCHECKS_MAX, KP41_MAX, BUGCHECK_PARAMS_MAX,
# EVENT_TS_MAX, BUGCHECK_PARAM_MAX) -- the agent clips to them, a test pins the two
_BUGCHECKS_MAX = 20
_KP41_MAX = 20
_BUGCHECK_PARAMS_MAX = 4
_EVENT_TS_MAX = 40
_BUGCHECK_PARAM_MAX = 18
_HEX = re.compile(r"0x[0-9a-fA-F]{1,16}")


def _dump_kind(path: Any) -> str:
    """mini / full / none / other -- the dump path itself never leaves the agent."""
    p = str(path or "").strip().lower()
    if not p:
        return "none"
    if "minidump" in p:
        return "mini"
    return "full" if p.endswith("memory.dmp") else "other"


def _bugcheck(item: Any) -> dict[str, Any]:
    """One 1001: its bug check line «0x0000009f (0x.., 0x.., 0x.., 0x..)» -> code 0x%08x
    (None when it is no 32-bit hex) and up to four hex parameters."""
    d = item if isinstance(item, dict) else {}
    hexes = _HEX.findall(str(d.get("raw") or ""))
    code = int(hexes[0], 16) if hexes else None
    params = [
        h.lower() for h in hexes[1 : 1 + _BUGCHECK_PARAMS_MAX] if len(h) <= _BUGCHECK_PARAM_MAX
    ]
    return {
        "ts": str(d.get("ts") or "")[:_EVENT_TS_MAX] or None,
        "code": f"0x{code:08x}" if code is not None and code <= 0xFFFFFFFF else None,
        "params": params,
        "dump": _dump_kind(d.get("dumpraw")),
    }


def _uint(value: Any) -> Optional[int]:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _kp41(item: Any) -> dict[str, Any]:
    """One Kernel-Power 41: the bug check code (0 = a power loss / reset, not a crash) and
    whether the power button was held (PowerButtonTimestamp != 0)."""
    d = item if isinstance(item, dict) else {}
    code, pb = _uint(d.get("code")), _uint(d.get("pb"))
    return {
        "ts": str(d.get("ts") or "")[:_EVENT_TS_MAX] or None,
        "bugcheck_code": code if code is not None and code <= 0xFFFFFFFF else None,
        "power_button": None if pb is None else pb != 0,
    }


def collect_historical(active_scan: bool = False) -> CollectorResult:
    result = run_ps(_SCRIPT, timeout=120)
    owned = [STORAGE_RELIABILITY, RELIABILITY, BOOT_TIME, CERTIFICATES, NETWORK]
    if result.status != "ok" or not isinstance(result.data, dict):
        status = result.status if result.status != "ok" else "partial"
        return CollectorResult(None, failed(owned, status, result.reason))
    raw = result.data
    raw["storage"] = as_list(raw.get("storage"))
    raw["bugchecks"] = [_bugcheck(b) for b in as_list(raw.get("bugchecks"))[:_BUGCHECKS_MAX]]
    raw["kp41"] = [_kp41(k) for k in as_list(raw.get("kp41"))[:_KP41_MAX]]
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
    merged_storage, smart_status, smart_reason = collect_smart(raw["storage"])
    raw["storage"] = merged_storage
    sh[SMART] = health(smart_status, smart_reason)

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
        sh[CERTIFICATES] = health(err_status, cert_res.reason)

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
