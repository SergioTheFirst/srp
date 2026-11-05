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
  # Перечисление журнала -- под своей защитой (журнала может не быть на этой SKU).
  $records = @()
  try { $records = @(Get-WinEvent -FilterHashtable $ht -MaxEvents $max -ErrorAction SilentlyContinue) } catch { $records = @() }
  # Защита НА КАЖДУЮ запись: у события с пустым TimeCreated бросает
  # ToUniversalTime(), у повреждённой -- Message/Id. Общий try вокруг цикла молча
  # отсекал ВСЕ последующие события, то есть главные улики по BSOD/крашам.
  foreach ($e in $records) {
    try {
      $lvl = switch ([int]$e.Level) {
        1 { 'Critical' } 2 { 'Error' } 3 { 'Warning' } 4 { 'Information' } 5 { 'Verbose' } default { 'Unknown' }
      }
      $msg = "$($e.Message)"
      $msg = ($msg -replace '\s+', ' ').Trim()
      if ($msg.Length -gt 600) { $msg = $msg.Substring(0, 600) }
      # Номер диска -- из пути \Device\HarddiskN\ в данных события (не из текста). Свой try:
      # сбой здесь не должен терять само событие.
      $dn = $null
      if ($e.ProviderName -eq 'disk') {
        try { if ("$($e.Properties[0].Value)" -match '\\Device\\Harddisk(\d+)\\') { $dn = [int]$Matches[1] } } catch {}
      }
      $out += [ordered]@{
        ts       = $e.TimeCreated.ToUniversalTime().ToString('o')
        log      = "$($e.LogName)"
        source   = "$($e.ProviderName)"
        event_id = [int]$e.Id
        level    = $lvl
        message  = $msg
        disk     = $dn
      }
    } catch { continue }
  }
  return $out
}

# Порядок = приоритет улик: при лимите 120 первыми остаются BSOD/питание/диск, не шум программ.
$queries = @(
  @{LogName='System'; ProviderName='Microsoft-Windows-Kernel-Power'; Id=41; StartTime=$start},
  @{LogName='System'; Id=6008; StartTime=$start},
  @{LogName='System'; ProviderName='Microsoft-Windows-WER-SystemErrorReporting'; Id=1001; StartTime=$start},
  @{LogName='System'; ProviderName='disk'; Id=7,11,51,52,153; StartTime=$start},
  @{LogName='System'; ProviderName='disk'; Id=157; StartTime=$start},
  @{LogName='System'; ProviderName='storahci'; Id=129; StartTime=$start},
  @{LogName='System'; ProviderName='stornvme'; Id=129; StartTime=$start},
  @{LogName='System'; ProviderName='Ntfs'; Id=55; StartTime=$start},
  @{LogName='System'; ProviderName='Microsoft-Windows-WHEA-Logger'; StartTime=$start},
  @{LogName='System'; ProviderName='Microsoft-Windows-WindowsUpdateClient'; Id=20,25,31; StartTime=$start},
  @{LogName='Application'; ProviderName='Application Error'; Id=1000; StartTime=$start},
  @{LogName='Application'; ProviderName='Application Hang'; Id=1002; StartTime=$start}
)

$all = @()
foreach ($q in $queries) { $all += Grab $q 40 }

# Лимит -- ДО сортировки по времени, иначе всплеск WHEA/падений программ вытесняет BugCheck 1001.
$all = @(@($all | Select-Object -First 120) | Sort-Object { $_.ts } -Descending)

# Шина диска на момент события: журнал подключений 1006 (последние 200). Берём только номер
# диска и шину -- числа; остальное в записи (модель, серийник, MBR) не читаем.
$arr = @()
if (@($all | Where-Object { $null -ne $_.disk }).Count) {
  $recs = @()
  try { $recs = @(Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-Partition/Diagnostic'; Id=1006} -MaxEvents 200 -ErrorAction SilentlyContinue) } catch { $recs = @() }
  foreach ($r in $recs) {
    try {
      # Регэксп по имени поля, не XML-разбор: у дешёвых флешек в серийнике управляющие символы
      # (0x1F, 0x06) -- разбор на них падает, а это ровно те записи, что нужны.
      $x = $r.ToXml()
      if ($x -match "<Data Name='DiskNumber'>(\d+)</Data>") { $num = [int]$Matches[1] } else { continue }
      if ($x -match "<Data Name='BusType'>(\d+)</Data>") { $bus = [int]$Matches[1] } else { continue }
      $arr += [ordered]@{ ts = $r.TimeCreated.ToUniversalTime().ToString('o'); disk = $num; bus = $bus }
    } catch { continue }
  }
}

[ordered]@{ events = @($all); arrivals = @($arr); window_hours = $H } | ConvertTo-Json -Depth 4 -Compress
"""

_REMOVABLE_BUSES = (4, 7)  # STORAGE_BUS_TYPE: 4 = 1394, 7 = USB


def _mark_removable(ev: Any, arrivals: list[Any]) -> Any:
    """disk-событие флешки/внешнего диска -> source "disk-removable": errchain его не считает.

    Решает последнее подключение (журнал 1006) того же номера диска не позже события --
    номера переиспользуются, текущий список дисков тут не годится. Нет совпадения или шина
    не съёмная -> событие как есть (считается, как раньше). Метка без «:» и стабильна между
    24-часовыми пересылками (дедуп сервера -- по (ts, source, event_id)).
    """
    if not isinstance(ev, dict) or ev.get("source") != "disk":
        return ev
    disk, ts = ev.get("disk"), ev.get("ts")
    if not isinstance(disk, int) or not isinstance(ts, str):
        return ev
    # ponytail: оба ts -- PowerShell ToString('o') в UTC: порядок строк = порядок времени.
    seen = [
        a
        for a in arrivals
        if isinstance(a, dict)
        and a.get("disk") == disk
        and isinstance(a.get("ts"), str)
        and a["ts"] <= ts
    ]
    if not seen or max(seen, key=lambda a: a["ts"]).get("bus") not in _REMOVABLE_BUSES:
        return ev
    return {**ev, "source": "disk-removable"}


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
        return CollectorResult(None, failed([EVENTS], status, result.reason))
    raw = result.data
    arrivals = as_list(raw.get("arrivals"))
    # _clean_event собирает запись из фиксированных ключей -- служебный "disk" на сервер не уходит.
    events = [
        e
        for e in (_clean_event(_mark_removable(x, arrivals)) for x in as_list(raw.get("events")))
        if e
    ]
    window = raw.get("window_hours")
    try:
        window = float(window) if window is not None else 24.0
    except (TypeError, ValueError):
        window = 24.0
    payload = {"events": events, "window_hours": window}
    return CollectorResult(payload, {EVENTS: health(field_status(bool(events)))})
