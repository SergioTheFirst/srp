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
# Штамп ДО запроса: события, попавшие в журнал во время самого запроса, иначе не
# попадут ни в этот проход, ни в следующий (окно потери шире окна повтора, а
# повтор дешёв -- сервер дедуплицирует по job_id).
$queried_at = (Get-Date).ToUniversalTime().ToString('o')
$jobs = @()
foreach ($e in Get-WinEvent -FilterHashtable $filter -MaxEvents 5000 -ErrorAction SilentlyContinue) {
    try {
        $p = $e.Properties
        $printer = if ($p.Count -gt 4) { "$($p[4].Value)" } else { '' }
        if (Test-Virtual $printer) { continue }
        $jid = $null
        # [0]=JobId, [1]=DocumentName (проверено на живом журнале ru-RU).
        if ($p.Count -gt 0) { try { $jid = [int]$p[0].Value } catch {} }
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
    } catch { continue }
}
[ordered]@{ jobs = @($jobs); queried_at = $queried_at } | ConvertTo-Json -Depth 3 -Compress
"""
    )


_DAILY_KEEP_DAYS = 62  # rolling per-day page map: two months covers the panel's "month"
# Сколько ДАВНИХ (не виденных в этом проходе) очередей переносим дальше: на
# терминальном сервере перенаправленные очереди («Printer (redirected 12)»)
# оставляют имена навсегда, и без обрезки state-файл рос бы вечно. Потолок мягкий:
# очереди ЭТОГО прохода не вытесняются никогда, поэтому на RDS с числом живых
# очередей больше потолка карта законно его превышает -- альтернатива (срезать
# живые) означала бы пере-сеед и потерю страниц. Реальная граница здесь --
# количество живых очередей, а не эта константа.
_MAX_BASELINES = 500
# Бюджет списка заданий в одном конверте. Ниже client.transport._MAX_PAYLOAD_BYTES
# (500 КБ) на размер метаданных конверта -- их считает уже транспорт.
_SWEEP_BUDGET_BYTES = 450_000


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


# Зеркало серверного капа shared/schema.py::PrintJobRecord (max_length=256) в стиле
# printer_ports._MAX_NAME_LEN: клип на агенте + reject на сервере. Без клипа имя
# >256 (misparse индекса события: DocumentName -- текст приложения, Windows его не
# ограничивает) роняет ВЕСЬ конверт 422-м, а watermark уже сдвинут = проход потерян.
_MAX_NAME_LEN = 256


def _parse_job(raw: Any) -> Optional[dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    printer = str(raw.get("printer") or "")[:_MAX_NAME_LEN]
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
        "user_name": (str(raw.get("user_name"))[:_MAX_NAME_LEN] if raw.get("user_name") else None),
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
# Перечисление CIM -- под своей защитой (класс отсутствует без службы печати).
# $ok отличает «класса нет / WMI разрушен» от «печатать было некому»: пустой
# список очередей сам по себе выглядит одинаково в обоих случаях, а это разные
# вещи -- UNKNOWN важнее ложной уверенности.
$ok = $true
$queues = @()
try { $queues = @(Get-CimInstance Win32_PerfFormattedData_Spooler_PrintQueue -ErrorAction Stop) } catch { $queues = @(); $ok = $false }
# Защита НА КАЖДУЮ очередь: одна битая запись раньше обрывала весь цикл, и
# страницы остальных принтеров терялись до следующего прохода.
foreach ($q in $queues) {
    try {
        $n = "$($q.Name)"
        if ($n -eq '_Total') { continue }
        # Начинаем с $null, а НЕ с нуля: ноль здесь означал бы «напечатано ноль
        # страниц», Python принимал бы его за перезапуск спулера и сбрасывал точку
        # отсчёта -- следующий проход отчитался бы за весь пробег принтера с завода.
        # Пропуск очереди безопасен: её базовая линия при этом сохраняется.
        # Внимание: [long]$null даёт 0 БЕЗ исключения, поэтому пустое значение
        # свойства проверяем явно, а не полагаемся на catch.
        $p = $null
        try { $p = [long]$q.TotalPagesPrinted } catch {}
        if ($null -eq $p) { continue }
        $rows += [ordered]@{ name = $n; pages = $p }
    } catch { continue }
}
[ordered]@{ queues = @($rows); ok = $ok } | ConvertTo-Json -Depth 3 -Compress
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

    Базовые линии очередей, не попавших в ЭТОТ проход (битая запись CIM,
    перезапуск спулера, временно отключённый принтер), сохраняются: иначе
    очередь пере-сеедится молча и страницы с прошлого прохода не сосчитаются
    никогда. Карта ограничена ``_MAX_BASELINES``.
    """
    jobs: list[dict[str, Any]] = []
    new_base: dict[str, int] = {}
    for queue in queues:
        # Клип ДО использования как ключа: обрезанное имя обязано совпадать со
        # своей же базовой линией прошлого прохода (иначе пере-сеед и потеря страниц).
        name = str(queue.get("name") or "")[:_MAX_NAME_LEN]
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
    # Потолок тратим на ДАВНИЕ очереди: увиденные в этом проходе не вытесняются
    # никогда (иначе они пере-сеедятся на следующем и их страницы не сосчитаются
    # вовсе -- на терминальном сервере живых очередей бывает больше потолка).
    # Порядок словаря = порядок последнего появления, поэтому берём хвост.
    absent = [(name, pages) for name, pages in baselines.items() if name not in new_base]
    room = max(0, _MAX_BASELINES - len(new_base))
    kept = dict(absent[-room:]) if room else {}
    kept.update(new_base)
    return jobs, kept


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


def _sweep_bytes(jobs: list[dict[str, Any]]) -> int:
    return len(json.dumps({"jobs": jobs}, ensure_ascii=False).encode("utf-8"))


def _cap_sweep(
    jobs: list[dict[str, Any]], sweep_ts: str, last_ts: str
) -> tuple[list[dict[str, Any]], str]:
    """Обрезать проход до размера, влезающего в конверт транспорта.

    Большой проход (5000 событий) перекрывает лимит конверта: транспорт отбросил бы
    ВЕСЬ проход, а водяной знак уже сдвинут -- потеря навсегда. Режем по ИЗМЕРЕННОМУ
    размеру, а не по числу заданий: кириллические имена принтера/пользователя втрое
    дороже ASCII, любая константа-счётчик была бы угадыванием. Держим самые СТАРЫЕ
    (Get-WinEvent отдаёт от новых к старым) и ставим знак на самое свежее сохранённое
    задание: остаток дочитает следующий проход.
    """
    kept = jobs
    # ponytail: половинное деление -- грубо (может срезать больше нужного), зато
    # ограничено ~12 итерациями и не требует модели размера записи.
    while len(kept) > 1 and _sweep_bytes(kept) > _SWEEP_BUDGET_BYTES:
        kept = kept[len(kept) // 2 :]  # срезаем самые НОВЫЕ, старые дочитаны раньше
    if len(kept) == len(jobs):
        return jobs, sweep_ts
    newest = max((_safe_ts(j.get("ts")) for j in kept), default="")
    if not newest or newest == last_ts:
        # Знак не сдвинулся бы -- следующий проход прочёл бы то же окно и обрезал так
        # же, навсегда. Двигаем на время запроса: потерять хвост честнее, чем встать.
        return kept, sweep_ts
    return kept, newest


def _collect_via_events(state_path: Path, state: dict[str, Any]) -> CollectorResult:
    """Event 307 sweep (per-job detail). last_sweep_ts semantics make the
    counter->events handoff naturally safe: 307 entries can only exist from
    the moment the log was (re)enabled, and pages up to the last counter sweep
    were already covered by deltas."""
    last_ts = _safe_ts(state.get("last_sweep_ts"))
    result = run_ps(_build_script(last_ts), timeout=90)
    if result.status != "ok" or not isinstance(result.data, dict):
        status = result.status if result.status != "ok" else "partial"
        return CollectorResult(None, failed([PRINT_JOBS], status))

    # Знак берём из PS (снят ДО запроса); fallback -- локальные часы.
    sweep_ts = _safe_ts(result.data.get("queried_at")) or datetime.now(timezone.utc).isoformat()
    jobs = [j for j in (_parse_job(x) for x in as_list(result.data.get("jobs"))) if j]
    jobs, sweep_ts = _cap_sweep(jobs, sweep_ts, last_ts)
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
    if result.data.get("ok") is False:
        # Скрипт отработал, но перечислить очереди не смог. Пустой список тогда
        # НЕ означает «никто не печатал» -- отчитаться исправным было бы ложной
        # уверенностью. Выходим до записи состояния: базовые линии на диске целы.
        return CollectorResult(None, failed([PRINT_JOBS], "partial"))

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
    if mode == "events":
        return _collect_via_events(state_path, state)
    sweep_ts = datetime.now(timezone.utc).isoformat()
    if autoenable:
        _try_enable_print_log()  # самолечение: этот sweep остаётся counter (журнал пока пуст)
    reseed = str(state.get("mode", "events")) != "counter"
    return _collect_via_counter(state_path, state, sweep_ts, reseed=reseed)
