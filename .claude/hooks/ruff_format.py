#!/usr/bin/env python3
"""PostToolUse hook: lint + format edited Python files with the repo's ruff.

Claude Code passes the tool payload as JSON on stdin. We pull out the edited
file path and, when it is a ``.py`` file, run ``ruff check --fix`` then
``ruff format`` on just that file using the active interpreter's ruff.

Design choices:
- Non-Python edits are ignored (exit 0).
- A missing ruff (not installed) is non-fatal -- we never block edits on
  tooling absence; ``make check`` and CI remain the authoritative gate.
- Output from ruff flows through so the user sees what changed.
"""

import json
import subprocess  # nosec B404 -- fixed argv (sys.executable + literals), no shell
import sys


def edited_path() -> str:
    """Return the file path from the tool payload, or '' if unavailable."""
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return ""
    tool_input = payload.get("tool_input") or {}
    return tool_input.get("file_path", "") or ""


def main() -> int:
    path = edited_path()
    if not path.endswith(".py"):
        return 0
    for argv in (
        [sys.executable, "-m", "ruff", "check", "--fix", path],
        [sys.executable, "-m", "ruff", "format", path],
    ):
        try:
            subprocess.run(argv, check=False)  # nosec B603 -- static argv, no user shell input
        except FileNotFoundError:
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
