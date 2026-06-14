# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the SRP deploy share (tray spec §6).

Three artifacts:
  * srp-agent  -- onedir, console   (the collector; runs as SYSTEM via schtasks)
  * srp-tray   -- onedir, windowed  (per-user status icon; same payload dir)
  * srp-setup  -- onefile, console, UAC-admin manifest (the installer)

Build:  python -m PyInstaller --clean --noconfirm packaging/srp.spec

onedir (not onefile) for agent/tray: instant start (no temp unpack), fewer AV
false-positives, robocopy-friendly delta upgrades. PyInstaller is a BUILD-only
dependency (requirements-build.txt); client/ stays pure stdlib at runtime.
"""

import os

ROOT = os.path.abspath(os.getcwd())
ASSETS = os.path.join("client", "tray", "assets")
_icons = [
    (os.path.join(ASSETS, name), ASSETS)
    for name in ("srp_ok.ico", "srp_warn.ico", "srp_alert.ico")
]

# --- agent: onedir, console ------------------------------------------------ #
agent_a = Analysis(  # noqa: F821
    ["srp_agent_main.py"],
    pathex=[ROOT],
    hiddenimports=[
        "client",
        "client.collectors.heartbeat",
        "client.collectors.historical",
        "client.collectors.inventory",
        "client.collectors.events",
        "client.collectors.print_jobs",
    ],
)
agent_pyz = PYZ(agent_a.pure)  # noqa: F821
agent_exe = EXE(  # noqa: F821
    agent_pyz,
    agent_a.scripts,
    [],
    exclude_binaries=True,
    name="srp-agent",
    console=True,
)
agent_coll = COLLECT(agent_exe, agent_a.binaries, agent_a.datas, name="agent")  # noqa: F821

# --- tray: onedir, windowed ------------------------------------------------ #
tray_a = Analysis(  # noqa: F821
    ["srp_tray_main.py"], pathex=[ROOT], datas=_icons, hiddenimports=["client"]
)
tray_pyz = PYZ(tray_a.pure)  # noqa: F821
tray_exe = EXE(  # noqa: F821
    tray_pyz,
    tray_a.scripts,
    [],
    exclude_binaries=True,
    name="srp-tray",
    console=False,
)
tray_coll = COLLECT(tray_exe, tray_a.binaries, tray_a.datas, name="tray")  # noqa: F821

# --- setup: onefile, console, UAC-admin ------------------------------------ #
setup_a = Analysis(["srp_setup_main.py"], pathex=[ROOT], hiddenimports=["client"])  # noqa: F821
setup_pyz = PYZ(setup_a.pure)  # noqa: F821
setup_exe = EXE(  # noqa: F821
    setup_pyz,
    setup_a.scripts,
    setup_a.binaries,
    setup_a.datas,
    [],
    name="srp-setup",
    console=True,
    uac_admin=True,
)
