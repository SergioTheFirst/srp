"""Print-job collector: reads Windows PrintService/Operational Event ID 307.

Sweeps events since the last successful run (stored in print_state.json next to
buffer.jsonl). Virtual printers are filtered in PowerShell and again in Python.
Pure stdlib — no external deps.
"""

from __future__ import annotations

import contextlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from client.collectors.ps import as_list, run_ps
from client.collectors.sources import PRINT_JOBS, CollectorResult, failed, field_status, health

_VIRTUAL = ("pdf", "xps", "fax", "onenote", "microsoft print to", "send to", "adobe", "docuworks")

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
$virtual = @('pdf','xps','fax','onenote','microsoft print to','send to','adobe','docuworks')
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


def _read_state(state_path: Path) -> str:
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return _safe_ts(data.get("last_sweep_ts"))
    except (OSError, json.JSONDecodeError, TypeError):
        return ""


def _write_state(state_path: Path, ts: str) -> None:
    with contextlib.suppress(OSError):
        state_path.write_text(json.dumps({"last_sweep_ts": ts}), encoding="utf-8")


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
    }


def collect_print_jobs(state_path: Path) -> CollectorResult:
    last_ts = _read_state(state_path)
    sweep_ts = datetime.now(timezone.utc).isoformat()

    result = run_ps(_build_script(last_ts), timeout=90)
    if result.status != "ok" or not isinstance(result.data, dict):
        status = result.status if result.status != "ok" else "partial"
        return CollectorResult(None, failed([PRINT_JOBS], status))

    jobs = [j for j in (_parse_job(x) for x in as_list(result.data.get("jobs"))) if j]
    _write_state(state_path, sweep_ts)

    payload = {"jobs": jobs, "window_from": last_ts or None}
    return CollectorResult(payload, {PRINT_JOBS: health(field_status(True))})
