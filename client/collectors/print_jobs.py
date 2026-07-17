"""Print-job collector: reads Windows PrintService/Operational Event ID 307.

Sweeps events since the last successful run (stored in print_state.json next to
buffer.jsonl). Virtual printers are filtered in PowerShell and again in Python.
Pure stdlib — no external deps.
"""

from __future__ import annotations

import contextlib
import json
import re
import subprocess  # nosec B404 -- фиксированный argv, shell=False; см. subprocess.run ниже
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from client.collectors.ps import NO_WINDOW, as_list, run_ps
from client.collectors.sources import PRINT_JOBS, CollectorResult, failed, field_status, health

_VIRTUAL = (
    "pdf",
    "xps",
    "fax",
    "onenote",
    "evernote",  # "Print to Evernote" -- seen live, not caught by the other entries
    "microsoft print to",
    "send to",
    "adobe",
    "docuworks",
)

# ISO-8601 timestamp regexp — only characters safe to embed into a PS string literal.
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.+\-Z]+$")


def _safe_ts(value: Optional[str]) -> str:
    """Return value only if it looks like an ISO timestamp; otherwise empty string."""
    if not value or not isinstance(value, str):
        return ""
    return value.strip() if _TS_RE.match(value.strip()) else ""


def _is_virtual(name: Optional[str]) -> bool:
    if not name:
        return False
    lower = name.lower()
    return any(v in lower for v in _VIRTUAL)


def _build_script(last_ts: str) -> str:
    ts_filter = (
        f"$filter.StartTime = [datetime]::Parse('{last_ts}').ToLocalTime()" if last_ts else ""
    )
    return (
        r"""
$filter = @{LogName='Microsoft-Windows-PrintService/Operational'; Id=307}
"""
        + ts_filter
        + r"""
$virtual = @('pdf','xps','fax','onenote','evernote','microsoft print to','send to','adobe','docuworks')
function Test-Virtual([string]$n) {
    $ln = $n.ToLower()
    foreach ($v in $virtual) { if ($ln.Contains($v)) { return $true } }
    return $false
}
$jobs = @()
try {
    foreach ($e in Get-WinEvent -FilterHashtable $filter -MaxEvents 2000 -ErrorAction SilentlyContinue) {
        $p = $e.Properties
        $printer = if ($p.Count -gt 4) { "$($p[4].Value)" } else { '' }
        if (Test-Virtual $printer) { continue }
        $jid = $null
        if ($p.Count -gt 1) { try { $jid = [int]$p[1].Value } catch {} }
        $pg = $null
        if ($p.Count -gt 7) { try { $pg = [int]$p[7].Value } catch {} }
        $sz = $null
        if ($p.Count -gt 6) { try { $sz = [long]$p[6].Value } catch {} }
        $un = if ($p.Count -gt 2) { "$($p[2].Value)" } else { $null }
        $jobs += [ordered]@{
            job_id     = $jid
            ts         = $e.TimeCreated.ToUniversalTime().ToString('o')
            printer    = $printer
            pages      = $pg
            size_bytes = $sz
            user_name  = $un
        }
    }
} catch {}
[ordered]@{ jobs = @($jobs) } | ConvertTo-Json -Depth 3 -Compress
"""
    )


_DAILY_KEEP_DAYS = 62  # rolling per-day page map: two months covers the panel's "month"


def _load_state(state_path: Path) -> dict[str, Any]:
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError):
        return {}


def _store_state(state_path: Path, state: dict[str, Any]) -> None:
    with contextlib.suppress(OSError):
        state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def accumulate_daily(
    state: dict[str, Any], jobs: list[dict[str, Any]], today_iso: str
) -> dict[str, Any]:
    """Return a NEW state with today's pages added to the rolling per-day map.

    Pages are credited to the *sweep* date: jobs land within one print
    interval of printing, so only the midnight edge can shift a job by one
    day -- negligible for the today/month panel counters. Entries older than
    ``_DAILY_KEEP_DAYS`` are pruned. The input state is not mutated.
    """
    daily: dict[str, int] = {}
    raw_daily = state.get("daily")
    if isinstance(raw_daily, dict):
        for day, pages in raw_daily.items():
            try:
                daily[str(day)] = int(pages)
            except (TypeError, ValueError):
                continue
    added = 0
    for job in jobs:
        pages = job.get("pages")
        if isinstance(pages, int) and pages > 0:
            added += pages
    if added:
        daily[today_iso] = daily.get(today_iso, 0) + added
    cutoff = (date.fromisoformat(today_iso) - timedelta(days=_DAILY_KEEP_DAYS)).isoformat()
    pruned = {day: pages for day, pages in daily.items() if day >= cutoff}
    return {**state, "daily": pruned}


def read_print_counters(state_path: Path, today: date) -> dict[str, Any]:
    """Today/month page totals + collection mode, for status.json (tray panel)."""
    state = _load_state(state_path)
    raw_daily = state.get("daily")
    daily = raw_daily if isinstance(raw_daily, dict) else {}
    today_key = today.isoformat()
    month_prefix = today_key[:7]
    today_pages = 0
    month_pages = 0
    for day, pages in daily.items():
        try:
            count = int(pages)
        except (TypeError, ValueError):
            continue
        if str(day).startswith(month_prefix):
            month_pages += count
        if str(day) == today_key:
            today_pages = count
    return {"today": today_pages, "month": month_pages, "mode": str(state.get("mode", "events"))}


def _parse_job(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    printer = raw.get("printer") or ""
    if _is_virtual(printer):
        return None
    pages = raw.get("pages")
    try:
        pages = int(pages) if pages is not None else None
    except (TypeError, ValueError):
        pages = None
    if not pages or pages <= 0:
        return None
    size = raw.get("size_bytes")
    try:
        size = int(size) if size is not None else None
    except (TypeError, ValueError):
        size = None
    job_id = raw.get("job_id")
    try:
        job_id = int(job_id) if job_id is not None else None
    except (TypeError, ValueError):
        job_id = None
    return {
        "job_id": job_id,
        "ts": raw.get("ts"),
        "printer": printer or None,
        "pages": pages,
        "size_bytes": size,
        "user_name": (raw.get("user_name") or None),
        "source": "events",
    }


# --------------------------------------------------------------------------- #
# Mode detection + counter fallback (tray spec §5)
# --------------------------------------------------------------------------- #

# Locale-safe: a single boolean leaves PowerShell, never localized text.
_MODE_SCRIPT = r"""
$enabled = $true
try {
    $log = Get-WinEvent -ListLog 'Microsoft-Windows-PrintService/Operational' -ErrorAction Stop
    $enabled = [bool]$log.IsEnabled
} catch { $enabled = $false }
[ordered]@{ enabled = $enabled } | ConvertTo-Json -Compress
"""

# CIM perf counter (project invariant: Win32_PerfFormattedData_*, not Get-Counter).
# TotalPagesPrinted is cumulative since spooler start; the synthetic "_Total"
# instance is the sum of all queues and must be skipped (double count).
_COUNTER_SCRIPT = r"""
$rows = @()
try {
    foreach ($q in Get-CimInstance Win32_PerfFormattedData_Spooler_PrintQueue -ErrorAction Stop) {
        $n = "$($q.Name)"
        if ($n -eq '_Total') { continue }
        $p = [long]0
        try { $p = [long]$q.TotalPagesPrinted } catch {}
        $rows += [ordered]@{ name = $n; pages = $p }
    }
} catch {}
[ordered]@{ queues = @($rows) } | ConvertTo-Json -Depth 3 -Compress
"""


def _detect_mode() -> str:
    """Pick the sweep mode: "events" | "counter".

    Counter only when the operational log is KNOWN to be disabled (or absent).
    If the check itself fails (PS broken), keep the old events behavior -- its
    own failure path reports the collector as blocked.
    """
    res = run_ps(_MODE_SCRIPT, timeout=30)
    if res.status == "ok" and isinstance(res.data, dict) and res.data.get("enabled") is False:
        return "counter"
    return "events"


_PRINT_LOG = "Microsoft-Windows-PrintService/Operational"
_ENABLE_ATTEMPTED = False  # 1 попытка на процесс агента: не бодаться с GPO каждый sweep


def _try_enable_print_log() -> None:
    """SYSTEM-агент включает операционный журнал печати, если он выключен.

    То же действие выполняет инсталлятор; здесь — самолечение уже развёрнутого
    парка (журнал бывает выключен GPO или на до-инсталляторных установках).
    Провал глотается: следующий sweep честно останется в counter-режиме.
    """
    global _ENABLE_ATTEMPTED
    if _ENABLE_ATTEMPTED:
        return
    _ENABLE_ATTEMPTED = True
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(  # nosec B603 B607 -- фиксированный argv, системная утилита
            ["wevtutil", "sl", _PRINT_LOG, "/e:true"],
            capture_output=True,
            timeout=15,
            creationflags=NO_WINDOW,
        )


def _counter_jobs(
    queues: list[dict[str, Any]], baselines: dict[str, int], sweep_ts: str
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Pure: per-queue page deltas vs baselines -> (job rows, new baselines).

    First sight of a queue seeds its baseline silently (its lifetime counter
    must not be emitted as "printed now"). A counter that went backwards means
    the spooler restarted: everything since the restart is real and uncounted,
    so the delta equals the current value. Virtual queues and "_Total" skipped.
    """
    jobs: list[dict[str, Any]] = []
    new_base: dict[str, int] = {}
    for queue in queues:
        name = str(queue.get("name") or "")
        if not name or name == "_Total" or _is_virtual(name):
            continue
        try:
            pages = int(queue.get("pages"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if pages < 0:
            continue
        new_base[name] = pages
        if name not in baselines:
            continue  # seed silently, no retro count
        base = baselines[name]
        delta = pages if pages < base else pages - base
        if delta > 0:
            jobs.append(
                {
                    "job_id": None,
                    "ts": sweep_ts,
                    "printer": name,
                    "pages": delta,
                    "size_bytes": None,
                    "user_name": None,
                    "source": "counter",
                }
            )
    return jobs, new_base


def _finish_sweep(
    state_path: Path,
    state: dict[str, Any],
    jobs: list[dict[str, Any]],
    sweep_ts: str,
    mode: str,
) -> None:
    """Accumulate daily counters and persist the post-sweep state."""
    new_state = accumulate_daily(state, jobs, datetime.now().date().isoformat())
    new_state["last_sweep_ts"] = sweep_ts
    new_state["mode"] = mode
    _store_state(state_path, new_state)


def _collect_via_events(state_path: Path, state: dict[str, Any], sweep_ts: str) -> CollectorResult:
    """Event 307 sweep (per-job detail). last_sweep_ts semantics make the
    counter->events handoff naturally safe: 307 entries can only exist from
    the moment the log was (re)enabled, and pages up to the last counter sweep
    were already covered by deltas."""
    last_ts = _safe_ts(state.get("last_sweep_ts"))
    result = run_ps(_build_script(last_ts), timeout=90)
    if result.status != "ok" or not isinstance(result.data, dict):
        status = result.status if result.status != "ok" else "partial"
        return CollectorResult(None, failed([PRINT_JOBS], status))

    jobs = [j for j in (_parse_job(x) for x in as_list(result.data.get("jobs"))) if j]
    _finish_sweep(state_path, state, jobs, sweep_ts, "events")
    payload = {"jobs": jobs, "window_from": last_ts or None}
    return CollectorResult(payload, {PRINT_JOBS: health(field_status(True))})


def _collect_via_counter(
    state_path: Path, state: dict[str, Any], sweep_ts: str, *, reseed: bool
) -> CollectorResult:
    """Spooler-counter sweep (page totals only, no user/document detail).

    *reseed* (entering counter mode from events): stored baselines are stale --
    pages printed during the events period were already counted via Event 307,
    so a delta against them would double-count. Drop them; this sweep seeds.
    """
    result = run_ps(_COUNTER_SCRIPT, timeout=60)
    if result.status != "ok" or not isinstance(result.data, dict):
        status = result.status if result.status != "ok" else "partial"
        return CollectorResult(None, failed([PRINT_JOBS], status))

    queues = [q for q in as_list(result.data.get("queues")) if isinstance(q, dict)]
    baselines: dict[str, int] = {}
    if not reseed and isinstance(state.get("baselines"), dict):
        for name, pages in state["baselines"].items():
            try:
                baselines[str(name)] = int(pages)
            except (TypeError, ValueError):
                continue
    jobs, new_baselines = _counter_jobs(queues, baselines, sweep_ts)
    state_with_base = {**state, "baselines": new_baselines}
    _finish_sweep(state_path, state_with_base, jobs, sweep_ts, "counter")
    payload = {"jobs": jobs, "window_from": None}
    return CollectorResult(payload, {PRINT_JOBS: health(field_status(True))})


def collect_print_jobs(state_path: Path, autoenable: bool = True) -> CollectorResult:
    """Sweep printed pages; the mode is re-decided EVERY sweep (self-healing).

    Log enabled -> events (rich per-job detail); disabled -> counter fallback.
    An admin enabling the log later upgrades the very next sweep with no
    double counting (see the transition notes on the helpers).
    """
    state = _load_state(state_path)
    mode = _detect_mode()
    sweep_ts = datetime.now(timezone.utc).isoformat()
    if mode == "events":
        return _collect_via_events(state_path, state, sweep_ts)
    if autoenable:
        _try_enable_print_log()  # самолечение: этот sweep остаётся counter (журнал пока пуст)
    reseed = str(state.get("mode", "events")) != "counter"
    return _collect_via_counter(state_path, state, sweep_ts, reseed=reseed)
