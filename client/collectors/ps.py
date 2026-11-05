"""Run a PowerShell snippet and parse its JSON output.

We shell out to Windows PowerShell (always present on a domain PC) instead of
depending on pywin32 -- the agent stays a near-zero-dependency drop-in. Scripts
are passed via ``-EncodedCommand`` (base64 UTF-16LE) so embedded quotes never
fight the shell, and output is forced to UTF-8 so Cyrillic event text survives.

``run_ps`` returns a :class:`PsResult` -- a collector-status plus parsed data --
so callers can tell *why* a source produced nothing (timeout / blocked / empty /
absent) rather than collapsing every failure to ``None``. A PC that cannot report
a signal must not look healthy: the status is reported, not swallowed.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess  # nosec B404
from typing import Any, NamedTuple, Optional

from client.winflags import NO_WINDOW

_PREAMBLE = (
    "$ProgressPreference='SilentlyContinue';"
    "$ErrorActionPreference='SilentlyContinue';"
    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
)

# CREATE_NO_WINDOW stops powershell.exe (a console app) from allocating a
# window -- essential for the per-user tray cert check and for any
# non-session-0 launch. NO_WINDOW re-exported here for existing importers
# (e.g. client/collectors/print_jobs.py); client/winflags.py is the source.
_NO_WINDOW = NO_WINDOW


# Постамбула (агент 0.3.5): упал последний оператор сниппета ($? = false) -> в stderr
# уходит FullyQualifiedErrorId последней ошибки, rc остаётся 1. Только язык и .NET --
# работает, когда модули PowerShell не грузятся (Dietnew: битый .NET в GAC). Преамбула
# глушит текст ошибок (SilentlyContinue), поэтому без постамбулы stderr пуст. Маркер
# собран в рантайме: эхо скрипта в CLIXML не должно дать ложный «SRPERR:». Ведущий \n
# несущий -- иначе хвостовой комментарий сниппета съест постамбулу.
_POSTAMBLE = (
    "\nif(-not $?){[Console]::Error.WriteLine('SRP'+'ERR:'+$Error[0].FullyQualifiedErrorId);exit 1}"
)
_MARKER = re.compile(r"(?m)^SRPERR:([^\r\n]*)")
REASON_MAX = 64  # = shared/schema.py SOURCE_REASON_MAX (пин в тестах)
_REASON_BAD_CHARS = re.compile(r"[^A-Za-z0-9_.-]")


class PsResult(NamedTuple):
    """Outcome of a PowerShell run.

    status: ok | empty | timeout | blocked | absent | partial
      ok      -- ran and returned parseable JSON (``data`` is set)
      empty   -- ran but produced no output
      partial -- ran, produced output, but it was not valid JSON
      timeout -- the call exceeded its timeout
      blocked -- an OS error prevented the call (policy / ACL / etc.)
      absent  -- powershell.exe was not found
    rc / reason -- только для пустого вывода: код выхода и ASCII-токен причины
      (FQID до запятой или ``rc_<код>``); rc 0 = законная пустота, reason None.
    """

    status: str
    data: Any = None
    rc: Optional[int] = None
    reason: Optional[str] = None


def sanitize_reason(value: Optional[str]) -> Optional[str]:
    """Токен причины: алфавит ``[A-Za-z0-9_.-]``, не длиннее REASON_MAX; без букв и
    цифр (кириллица, пусто) -> None. Тот же алфавит чистит сервер."""
    token = _REASON_BAD_CHARS.sub("_", (value or "").strip())[:REASON_MAX]
    return token if re.search(r"[A-Za-z0-9]", token) else None


def _reason_from(rc: Optional[int], stderr: Optional[bytes]) -> Optional[str]:
    """Причина пустого вывода: FQID из маркера постамбулы до первой запятой, а без
    маркера (CLM, parse error, хост упал до скрипта) -- ``rc_<код>``."""
    if not rc:
        return None
    marker = _MARKER.search((stderr or b"").decode("latin-1"))
    token = sanitize_reason(marker.group(1).split(",", 1)[0]) if marker else None
    return token or sanitize_reason(f"rc_{rc}")


def run_ps(script: str, timeout: int = 30) -> PsResult:
    """Execute *script* in PowerShell; return a PsResult (status + parsed JSON)."""
    encoded = base64.b64encode((_PREAMBLE + script + _POSTAMBLE).encode("utf-16-le")).decode(
        "ascii"
    )
    try:
        # B603/B607: static argv, no shell; "powershell" resolved from PATH, no user input.
        proc = subprocess.run(  # nosec B603 B607
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            capture_output=True,
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
    except FileNotFoundError:
        return PsResult("absent")
    except subprocess.TimeoutExpired:
        return PsResult("timeout")
    except OSError:
        return PsResult("blocked")

    out = proc.stdout.decode("utf-8", errors="replace").strip()
    if not out:
        rc = proc.returncode
        return PsResult("empty", rc=rc, reason=_reason_from(rc, proc.stderr))
    try:
        return PsResult("ok", json.loads(out))
    except json.JSONDecodeError:
        return PsResult("partial")


def as_list(value: Any) -> list[Any]:
    """Normalize ConvertTo-Json's single-object-vs-array quirk into a list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]
