"""Events: a whitelisted batch of recent high-signal Windows log entries.

Unlike ``historical`` (which only *counts* failure events over 30 days), this
collector ships the individual records from the last 24h so the server can show
*what* actually happened, not just how often. The whitelist is deliberately
narrow -- power loss, dirty shutdown, bugcheck, corrected-hardware (WHEA), disk
and NTFS errors, app crashes, and Windows Update install/download failures (the
downstream symptom of a disk-fill / servicing collapse, W4.2) -- to keep the
payload small and the signal high.

ssd3 Ф3 (T3.1) adds four more entries: storage port-driver retries the plain
``disk`` provider doesn't see (``storahci``/``stornvme`` id 129 -- AHCI/NVMe
stack resets, not the disk class driver), disk id 157 (surprise removal --
collected for raw visibility, not yet wired into any coordinate), and
Application Hang (1002), which feeds the Bayesian stability class directly
(server/analytics/errchain.py deliberately excludes it from the storage
causal chain).

Locale note: ``LevelDisplayName`` is localized (a Russian box returns
"Ошибка", not "Error"), so we map the *numeric* ``$e.Level`` to a stable English
string in PowerShell. Likewise ``Id`` arrays in ``-FilterHashtable`` let one
query cover several disk error codes without locale-sensitive text matching.
"""

from __future__ import annotations

from typing import Any, Optional

from client.collectors.ps import as_list, run_ps
from client.collectors.sources import EVENTS, CollectorResult, failed, field_status, health

_MAX_MESSAGE = 500  # server truncates to this too; clamp early to bound payload

_SCRIPT = r"""
$H = 24
$start = (Get-Date).AddHours(-$H)

function Grab($ht, $max) {
  $out = @()
  try {
    foreach ($e in Get-WinEvent -FilterHashtable $ht -MaxEvents $max -ErrorAction SilentlyContinue) {
      $lvl = switch ([int]$e.Level) {
        1 { 'Critical' } 2 { 'Error' } 3 { 'Warning' } 4 { 'Information' } 5 { 'Verbose' } default { 'Unknown' }
      }
      $msg = "$($e.Message)"
      $msg = ($msg -replace '\s+', ' ').Trim()
      if ($msg.Length -gt 600) { $msg = $msg.Substring(0, 600) }
      $out += [ordered]@{
        ts       = $e.TimeCreated.ToUniversalTime().ToString('o')
        log      = "$($e.LogName)"
        source   = "$($e.ProviderName)"
        event_id = [int]$e.Id
        level    = $lvl
        message  = $msg
      }
    }
  } catch {}
  return $out
}

$queries = @(
  @{LogName='System'; ProviderName='Microsoft-Windows-Kernel-Power'; Id=41; StartTime=$start},
  @{LogName='System'; Id=6008; StartTime=$start},
  @{LogName='System'; ProviderName='Microsoft-Windows-WER-SystemErrorReporting'; Id=1001; StartTime=$start},
  @{LogName='System'; ProviderName='Microsoft-Windows-WHEA-Logger'; StartTime=$start},
  @{LogName='System'; ProviderName='disk'; Id=7,11,51,52,153; StartTime=$start},
  @{LogName='System'; ProviderName='disk'; Id=157; StartTime=$start},
  @{LogName='System'; ProviderName='storahci'; Id=129; StartTime=$start},
  @{LogName='System'; ProviderName='stornvme'; Id=129; StartTime=$start},
  @{LogName='System'; ProviderName='Ntfs'; Id=55; StartTime=$start},
  @{LogName='System'; ProviderName='Microsoft-Windows-WindowsUpdateClient'; Id=20,25,31; StartTime=$start},
  @{LogName='Application'; ProviderName='Application Error'; Id=1000; StartTime=$start},
  @{LogName='Application'; ProviderName='Application Hang'; Id=1002; StartTime=$start}
)

$all = @()
foreach ($q in $queries) { $all += Grab $q 40 }

$all = $all | Sort-Object { $_.ts } -Descending | Select-Object -First 120

[ordered]@{ events = @($all); window_hours = $H } | ConvertTo-Json -Depth 4 -Compress
"""


def _clean_event(ev: Any) -> Optional[dict[str, Any]]:
    if not isinstance(ev, dict):
        return None
    msg = ev.get("message")
    if isinstance(msg, str) and len(msg) > _MAX_MESSAGE:
        msg = msg[:_MAX_MESSAGE]
    eid = ev.get("event_id")
    try:
        eid = int(eid) if eid is not None else None
    except (TypeError, ValueError):
        eid = None
    return {
        "ts": ev.get("ts"),
        "log": ev.get("log") or None,
        "source": ev.get("source") or None,
        "event_id": eid,
        "level": ev.get("level") or None,
        "message": msg or None,
    }


def collect_events() -> CollectorResult:
    result = run_ps(_SCRIPT, timeout=60)
    if result.status != "ok" or not isinstance(result.data, dict):
        status = result.status if result.status != "ok" else "partial"
        return CollectorResult(None, failed([EVENTS], status))
    raw = result.data
    events = [e for e in (_clean_event(x) for x in as_list(raw.get("events"))) if e]
    window = raw.get("window_hours")
    try:
        window = float(window) if window is not None else 24.0
    except (TypeError, ValueError):
        window = 24.0
    payload = {"events": events, "window_hours": window}
    return CollectorResult(payload, {EVENTS: health(field_status(bool(events)))})
