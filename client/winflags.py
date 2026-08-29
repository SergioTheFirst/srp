"""Single Windows subprocess-creation-flag constant, shared across client/.

Kept as its own tiny stdlib-only module (not folded into a package like
``collectors``) so the elevated installer (``client/deploy/setup.py``,
UAC-elevated) does not need to import the telemetry-collectors package --
with its network/printer/user-cert/event-log submodules -- just for one flag.
"""

from __future__ import annotations

import subprocess  # nosec B404

# The agent must run silently: no console window may flash on the user's
# desktop on any subprocess launch (PowerShell, wevtutil, robocopy, the tray
# child processes, ...). Absent off-Windows, where 0 is the harmless default.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
