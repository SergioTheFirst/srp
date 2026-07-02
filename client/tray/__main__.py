"""Tray entry point (tray spec §1-§4): ``srp-tray`` / ``python -m client.tray``.

Modes:
  * default          -- run the tray icon (Windows only; blocks on the message loop)
  * ``--panel``      -- show the info panel, then exit (launched as a child process)
  * ``--ask-password`` -- modal password prompt; exit 0 if correct (used by Exit)

Single instance is enforced with a named mutex. The icon, panel and password
prompt never share a thread: the panel/prompt run as separate ``srp-tray``
child processes so a tkinter crash cannot drop the icon. The personal-cert check
(:mod:`client.tray.certs`) runs on a 30-minute cadence inside the icon loop and
drives the icon colour + expiry balloons; the panel does its own read-only check.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os

# subprocess: only ever launches this same exe / `-m client.tray` with fixed
# flags (--panel / --ask-password); no shell, no caller- or user-supplied argv.
import subprocess  # nosec B404
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from client.tray import certs as cz
from client.tray import panel, spool
from client.tray import state as st
from client.winflags import NO_WINDOW

log = logging.getLogger("client.tray")

_POLL_MS = 60_000  # re-read status.json + recompute the icon once a minute
_CERT_INTERVAL_SEC = 30 * 60  # re-check the personal certificate every 30 min (spec §3)
_MUTEX_NAME = "Local\\SRPTrayInstanceMutex"


# --------------------------------------------------------------------------- #
# Paths (frozen-aware; mirror status_writer without config.py's write side effect)
# --------------------------------------------------------------------------- #


def _config_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "config.json"
    return Path(__file__).resolve().parents[1] / "config.json"


def _status_path(config_path: Path) -> Path:
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        buf = (
            str(data.get("buffer_path", "buffer.jsonl"))
            if isinstance(data, dict)
            else "buffer.jsonl"
        )
    except (OSError, ValueError):
        buf = "buffer.jsonl"
    p = Path(buf)
    base = p if p.is_absolute() else config_path.parent / p
    return base.with_name("status.json")


def _appdata_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "SRP"


def _read_cert_config(config_path: Path) -> tuple[int, int, bool, str]:
    """(warn_days, notify_hours, require_cert, helpdesk) read-only from config.json.

    Mirrors :func:`panel.read_config_bits`: never triggers config.py's device_id
    persist side effect, and degrades to spec §3 defaults when the file is missing
    or ACL-blocked (the tray runs as an ordinary user).
    """
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return (
        int(data.get("tray_cert_warn_days", 14) or 14),
        int(data.get("tray_notify_hours", 4) or 4),
        bool(data.get("tray_require_cert", False)),
        str(data.get("helpdesk_contact", "")),
    )


def _panel_cert_text(config_path: Path) -> str:
    """Live, read-only certificate row for the panel (no nag, no state write)."""
    warn_days, notify_hours, require_cert, helpdesk = _read_cert_config(config_path)
    result = cz.evaluate(
        cz.query_certs(),
        {},
        now=time.time(),
        warn_days=warn_days,
        notify_hours=notify_hours,
        require_cert=require_cert,
        helpdesk=helpdesk,
    )
    return result.panel_text


def _setup_logging() -> None:
    try:
        d = _appdata_dir()
        d.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = RotatingFileHandler(
            d / "tray.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
    except OSError:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


# --------------------------------------------------------------------------- #
# Tray controller
# --------------------------------------------------------------------------- #


class _TrayApp:
    def __init__(self, status_path: Path, config_path: Path, tray_state_path: Path) -> None:
        from client.tray.icon import TrayIcon

        self.status_path = status_path
        self.config_path = config_path
        self.tray_state_path = tray_state_path
        (
            self._warn_days,
            self._notify_hours,
            self._require_cert,
            self._helpdesk,
        ) = _read_cert_config(config_path)
        self._cert_level: st.CertLevel = "unknown"
        self._next_cert_check = 0.0  # check on the first refresh, then every 30 min
        self.icon = TrayIcon(
            on_open=self.open_panel,
            on_refresh=self.refresh,
            on_about=self.about,
            on_exit=self.request_exit,
        )

    def _child(self, *flags: str) -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable, *flags]
        return [sys.executable, "-m", "client.tray", *flags]

    def open_panel(self) -> None:
        subprocess.Popen(self._child("--panel"), creationflags=NO_WINDOW)  # nosec B603

    def about(self) -> None:
        self.icon.balloon(
            "О программе SRP",
            "Иконка состояния SRP. Пароль защищает от случайного закрытия "
            "обычным пользователем, не от администратора.",
            "ok",
        )

    def _maybe_check_certs(self, now: float) -> None:
        """Re-check the personal cert at most every 30 min; nag + cache the level.

        Blocks the message loop for the PowerShell call (<1 s, twice an hour) --
        acceptable versus the complexity of a worker thread at PC-fleet scale.
        """
        if now < self._next_cert_check:
            return
        self._next_cert_check = now + _CERT_INTERVAL_SEC
        try:
            prior = st.load_cert_state(self.tray_state_path)
            certs = cz.query_certs()
            result = cz.evaluate(
                certs,
                prior,
                now=now,
                warn_days=self._warn_days,
                notify_hours=self._notify_hours,
                require_cert=self._require_cert,
                helpdesk=self._helpdesk,
            )
            self._cert_level = result.level
            if result.state != prior:
                st.save_cert_state(self.tray_state_path, result.state)
            for b in result.balloons:
                self.icon.balloon(b.title, b.message, b.level)
            # stage 8: spool these personal certs so the SYSTEM agent (which can't see
            # CurrentUser\My) can surface them in the fleet. None = PS failed -> skipped.
            spool.publish_user_certs(certs)
        except Exception:  # a cert hiccup must never drop the icon
            log.exception("certificate check failed")

    def refresh(self) -> None:
        now = time.time()
        self._maybe_check_certs(now)
        view = st.read_status(self.status_path, now=now)
        age = view.age_sec if view else None
        link = view.link_age_sec if view else None
        icon_state = st.icon_state(self._cert_level, agent_age_sec=age, link_age_sec=link)
        tooltip = st.tooltip(view, icon_state) if view else "SRP · агент не отвечает"
        self.icon.show(icon_state, tooltip)

    def request_exit(self) -> None:
        proc = subprocess.run(self._child("--ask-password"), creationflags=NO_WINDOW)  # nosec B603
        if proc.returncode == 0:
            self.icon.post_quit()

    def run(self) -> None:
        self.icon.set_timer(_POLL_MS, self.refresh)
        self.refresh()
        self.icon.run()
        self.icon.remove()


def _acquire_single_instance() -> Optional[int]:
    """Hold a named mutex for this process; None if another instance owns it."""
    k32 = ctypes.windll.kernel32
    handle = k32.CreateMutexW(None, False, _MUTEX_NAME)
    if st.single_instance_should_exit(k32.GetLastError()):
        return None
    return int(handle)


# --------------------------------------------------------------------------- #
# Entry
# --------------------------------------------------------------------------- #


def _parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="srp-tray", description="SRP tray client")
    p.add_argument("--panel", action="store_true", help="show the status panel and exit")
    p.add_argument(
        "--ask-password", action="store_true", help="modal password prompt (exit 0 if ok)"
    )
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    config_path = _config_path()
    status_path = _status_path(config_path)
    tray_state_path = _appdata_dir() / "tray_state.json"

    if args.panel:
        panel.run_panel(
            status_path=status_path,
            config_path=config_path,
            cert_text=_panel_cert_text(config_path),
        )
        return 0
    if args.ask_password:
        return panel.run_password_prompt(config_path=config_path, tray_state_path=tray_state_path)

    _setup_logging()
    try:
        mutex = _acquire_single_instance()
    except AttributeError:
        log.error("The tray requires Windows.")
        return 2
    if mutex is None:
        log.info("Another tray instance is already running; exiting.")
        return 0
    try:
        _TrayApp(status_path, config_path, tray_state_path).run()
    except RuntimeError as exc:
        log.error("%s", exc)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
