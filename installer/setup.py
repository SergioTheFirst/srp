"""srp-setup.exe — single-file installer for the SRP agent.

Built by build.bat via PyInstaller with the agent binary and config template
already embedded.  Run as Administrator on any Windows 7+ machine, no Python
or internet connection required.

Usage:
    srp-setup.exe                         # interactive: prompts for server URL
    srp-setup.exe --server http://x:8000  # silent: no prompt, exits 0 on success
    srp-setup.exe --uninstall             # stop + remove task and files
"""

from __future__ import annotations

import argparse
import ctypes
import json
import shutil
import subprocess
import sys
from pathlib import Path

INSTALL_DIR = Path("C:/SRP")
TASK_NAME = "SRP Agent"
EXE_NAME = "srp-agent.exe"
LOG_NAME = "srp-agent.log"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _resource(name: str) -> Path:
    """Return path to a file embedded in the bundle (or next to this script)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / name


def _ps(script: str) -> None:
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        check=True,
    )


def _enable_print_log() -> None:
    """Enable Microsoft-Windows-PrintService/Operational so Event 307 is written.

    Non-fatal: a warning is printed if the call fails (e.g. restricted policy),
    but the rest of the install continues. The print queue clears normally after
    each job — this log is invisible to end users.
    """
    script = (
        "$log = Get-WinEvent -ListLog 'Microsoft-Windows-PrintService/Operational';"
        "if (-not $log.IsEnabled) { $log.IsEnabled = $true; $log.SaveChanges();"
        "  Write-Host '  Print log enabled.' }"
        "else { Write-Host '  Print log already enabled.' }"
    )
    try:
        _ps(script)
    except Exception as exc:  # noqa: BLE001
        print(f"  WARNING: could not enable print log ({exc}); print tracking may not work")


# --------------------------------------------------------------------------- #
# Install / uninstall
# --------------------------------------------------------------------------- #


def install(server_url: str, token: str = "") -> None:
    # Stop any running instance before overwriting the binary
    subprocess.run(["schtasks", "/end", "/tn", TASK_NAME], capture_output=True)

    INSTALL_DIR.mkdir(parents=True, exist_ok=True)

    # Enable Windows print-job event log (Event 307) so the agent can collect it.
    # Invisible to end users — the print queue still clears after each job as normal.
    _enable_print_log()

    # Copy agent binary
    shutil.copy2(_resource(EXE_NAME), INSTALL_DIR / EXE_NAME)
    print(f"  {INSTALL_DIR / EXE_NAME}")

    # Config: write fresh one (or keep existing to preserve device_id)
    cfg_path = INSTALL_DIR / "config.json"
    if cfg_path.exists():
        # Update only server_url / ingest_token; leave device_id intact
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["server_url"] = server_url
        if token:
            cfg["ingest_token"] = token
        cfg_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  {cfg_path}  (device_id preserved)")
    else:
        # Fresh install: copy template and inject URL
        template = json.loads(_resource("config.json").read_text(encoding="utf-8"))
        template["server_url"] = server_url
        if token:
            template["ingest_token"] = token
        cfg_path.write_text(json.dumps(template, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  {cfg_path}")

    # Register scheduled task: SYSTEM, on boot, restart on failure x3
    exe_str = str(INSTALL_DIR / EXE_NAME).replace("\\", "\\\\")
    log_str = str(INSTALL_DIR / LOG_NAME).replace("\\", "\\\\")
    _ps(
        f"$a = New-ScheduledTaskAction -Execute '{exe_str}'"
        f" -Argument '--log-file \"{log_str}\"';"
        f"$t = New-ScheduledTaskTrigger -AtStartup;"
        f"$s = New-ScheduledTaskSettingsSet -RestartCount 3"
        f" -RestartInterval (New-TimeSpan -Minutes 5) -StartWhenAvailable $true;"
        f"Register-ScheduledTask -TaskName '{TASK_NAME}'"
        f" -Action $a -Trigger $t -Settings $s"
        f" -RunLevel Highest -User 'SYSTEM' -Force | Out-Null"
    )
    print(f'  Task "{TASK_NAME}" registered (SYSTEM, boot, restart x3)')

    # Start immediately — no reboot required
    subprocess.run(["schtasks", "/run", "/tn", TASK_NAME], capture_output=True)
    print(f'  Started "{TASK_NAME}"')


def uninstall() -> None:
    subprocess.run(["schtasks", "/end", "/tn", TASK_NAME], capture_output=True)
    subprocess.run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"], capture_output=True)
    if INSTALL_DIR.exists():
        shutil.rmtree(INSTALL_DIR)
    print(f'Removed task "{TASK_NAME}" and {INSTALL_DIR}')


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def main() -> None:
    parser = argparse.ArgumentParser(prog="srp-setup", add_help=True)
    parser.add_argument(
        "--server", metavar="URL", help="SRP server URL, e.g. http://192.168.1.10:8000"
    )
    parser.add_argument(
        "--token", metavar="TOKEN", help="ingest token (leave empty if server auth is off)"
    )
    parser.add_argument("--uninstall", action="store_true", help="stop and remove the agent")
    args = parser.parse_args()

    if not _is_admin():
        print("ERROR: Run as Administrator.")
        if not args.server:  # interactive mode: wait before closing window
            input("Press Enter to exit…")
        sys.exit(1)

    if args.uninstall:
        uninstall()
        return

    server_url = args.server or ""
    if not server_url:
        server_url = input("Server URL (e.g. http://192.168.1.10:8000): ").strip()
    if not server_url:
        print("ERROR: server URL is required.")
        input("Press Enter to exit…")
        sys.exit(1)

    token = args.token or ""

    print(f"\nInstalling SRP Agent → {INSTALL_DIR}")
    try:
        install(server_url, token)
    except Exception as exc:
        print(f"\nERROR: {exc}")
        input("Press Enter to exit…")
        sys.exit(1)

    print("\nDONE")
    if not args.server:  # interactive: keep window open
        input("Press Enter to exit…")


if __name__ == "__main__":
    main()
