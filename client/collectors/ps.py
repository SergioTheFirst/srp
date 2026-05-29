"""Run a PowerShell snippet and parse its JSON output.

We shell out to Windows PowerShell (always present on a domain PC) instead of
depending on pywin32 -- the agent stays a near-zero-dependency drop-in. Scripts
are passed via ``-EncodedCommand`` (base64 UTF-16LE) so embedded quotes never
fight the shell, and output is forced to UTF-8 so Cyrillic event text survives.

Every helper is failure-tolerant: a missing source, a blocked cmdlet, or a
timeout returns ``None``/empty rather than raising -- a PC that can't report a
signal must not crash the agent (and, upstream, must not look healthy).
"""

from __future__ import annotations

import base64
import json
import subprocess  # nosec B404
from typing import Any

_PREAMBLE = (
    "$ProgressPreference='SilentlyContinue';"
    "$ErrorActionPreference='SilentlyContinue';"
    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
)


def run_ps(script: str, timeout: int = 30) -> Any:
    """Execute *script* in PowerShell; return parsed JSON, or None on any failure."""
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
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None

    out = proc.stdout.decode("utf-8", errors="replace").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def as_list(value: Any) -> list[Any]:
    """Normalize ConvertTo-Json's single-object-vs-array quirk into a list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]
