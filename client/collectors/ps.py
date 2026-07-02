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
import subprocess  # nosec B404
from typing import Any, NamedTuple

_PREAMBLE = (
    "$ProgressPreference='SilentlyContinue';"
    "$ErrorActionPreference='SilentlyContinue';"
    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
)

# The agent must collect silently: no console window may flash on the user's
# desktop on any sweep. CREATE_NO_WINDOW stops powershell.exe (a console app)
# from allocating a window -- essential for the per-user tray cert check and for
# any non-session-0 launch. Absent off-Windows, where 0 is the harmless default.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
NO_WINDOW = _NO_WINDOW  # public: единый флаг «без окна» для ВСЕХ subprocess в client/


class PsResult(NamedTuple):
    """Outcome of a PowerShell run.

    status: ok | empty | timeout | blocked | absent | partial
      ok      -- ran and returned parseable JSON (``data`` is set)
      empty   -- ran but produced no output
      partial -- ran, produced output, but it was not valid JSON
      timeout -- the call exceeded its timeout
      blocked -- an OS error prevented the call (policy / ACL / etc.)
      absent  -- powershell.exe was not found
    """

    status: str
    data: Any = None


def run_ps(script: str, timeout: int = 30) -> PsResult:
    """Execute *script* in PowerShell; return a PsResult (status + parsed JSON)."""
    encoded = base64.b64encode((_PREAMBLE + script).encode("utf-16-le")).decode("ascii")
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
        return PsResult("empty")
    try:
        return PsResult("ok", json.loads(out))
    except json.JSONDecodeError:
        return PsResult("partial")


def as_list(value: Any) -> list[Any]:
    """Normalize ConvertTo-Json's single-object-vs-array quirk into a list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]
