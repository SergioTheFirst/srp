"""Windows subprocess-creation flag + build bitness, shared across client/.

Kept as its own tiny stdlib-only module (not folded into a package like
``collectors``) so the elevated installer (``client/deploy/setup.py``,
UAC-elevated) does not need to import the telemetry-collectors package --
with its network/printer/user-cert/event-log submodules -- just for one flag.
"""

from __future__ import annotations

import struct
import subprocess  # nosec B404

# The agent must run silently: no console window may flash on the user's
# desktop on any subprocess launch (PowerShell, wevtutil, robocopy, the tray
# child processes, ...). Absent off-Windows, where 0 is the harmless default.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 0.3.6: bitness of THIS build (the Python it was frozen with), not of the OS: the x86
# build asks the server for the x86 package and reports it in every envelope.
AGENT_ARCH = "x86" if struct.calcsize("P") * 8 == 32 else "x64"
