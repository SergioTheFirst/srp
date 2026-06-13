"""Pure decision logic for the tray (tray spec §2 + §4).

Everything the tray must *decide* -- icon colour, agent/link freshness, the
support clipboard line, panel row text, and the password gate -- lives here as
pure functions so it is unit-testable off-Windows. The ctypes/tkinter adapters
(:mod:`client.tray.icon`, :mod:`client.tray.panel`) carry no logic of their own.

Reads two files, never writes the agent's: ``status.json`` (the agent's one-way
status, no secrets) and ``tray_state.json`` in ``%LOCALAPPDATA%\\SRP`` (the tray's
own anti-spam + lockout store).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

from client.config import verify_password

log = logging.getLogger(__name__)

IconState = Literal["ok", "warn", "alert"]
AgentFreshness = Literal["alive", "silent", "dead"]
CertLevel = Literal["ok", "warn", "alert", "unknown"]

# Freshness thresholds (spec §2 / §1: 15-min freshness window).
_SILENT_AFTER_SEC = 15 * 60  # agent has not written status in 15 min -> "molchit"
_DEAD_AFTER_SEC = 24 * 60 * 60  # ... in over a day -> presumed dead (red)
_LINK_WARN_SEC = 60 * 60  # no successful send in over an hour -> warn

# Password lockout (spec §4).
_LOCKOUT_FAILS = 3
_LOCKOUT_SEC = 5 * 60

# Panel thresholds.
_DISK_WARN_GB = 10.0  # spec §2 says <10%; status.json carries GB only -> absolute v1
_UPTIME_HINT_DAYS = 14.0

# Windows: CreateMutexW sets ERROR_ALREADY_EXISTS when another instance holds it.
_ERROR_ALREADY_EXISTS = 183

_GATE_KEY = "_gate"


# --------------------------------------------------------------------------- #
# status.json view
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class StatusView:
    """A parsed ``status.json`` plus the ages derived at read time."""

    raw: dict[str, Any]
    age_sec: Optional[float]  # now - ts; None if ts unreadable
    link_age_sec: Optional[float]  # now - last_send_ok_ts; None if never sent

    def _s(self, key: str, default: str = "") -> str:
        val = self.raw.get(key, default)
        return str(val) if val is not None else default

    def _i(self, key: str) -> int:
        try:
            return int(self.raw.get(key, 0) or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def hostname(self) -> str:
        return self._s("hostname", "—")

    @property
    def ips(self) -> list[str]:
        raw = self.raw.get("ips") or []
        return [str(x) for x in raw] if isinstance(raw, list) else []

    @property
    def org_code(self) -> str:
        return self._s("org_code")

    @property
    def dept_code(self) -> str:
        return self._s("dept_code")

    @property
    def agent_version(self) -> str:
        return self._s("agent_version", "?")

    @property
    def last_error(self) -> str:
        return self._s("last_send_error")

    @property
    def last_send_ok_ts(self) -> Optional[int]:
        val = self.raw.get("last_send_ok_ts")
        try:
            return int(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def buffer_depth(self) -> int:
        return self._i("buffer_depth")

    @property
    def print_today(self) -> int:
        return self._i("print_today_pages")

    @property
    def print_month(self) -> int:
        return self._i("print_month_pages")

    @property
    def print_mode(self) -> str:
        return self._s("print_mode", "events")

    @property
    def disk_free_gb(self) -> Optional[float]:
        val = self.raw.get("disk_free_gb")
        return float(val) if isinstance(val, (int, float)) else None

    @property
    def uptime_days(self) -> Optional[float]:
        val = self.raw.get("uptime_days")
        return float(val) if isinstance(val, (int, float)) else None


def read_status(path: Path, *, now: float) -> Optional[StatusView]:
    """Parse ``status.json``; return None on a missing or corrupt file."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    ts = doc.get("ts")
    age = (now - float(ts)) if isinstance(ts, (int, float)) else None
    ok_ts = doc.get("last_send_ok_ts")
    link_age = (now - float(ok_ts)) if isinstance(ok_ts, (int, float)) else None
    return StatusView(raw=doc, age_sec=age, link_age_sec=link_age)


# --------------------------------------------------------------------------- #
# freshness + icon worst-of (spec §2)
# --------------------------------------------------------------------------- #


def agent_freshness(age_sec: Optional[float]) -> AgentFreshness:
    """alive < 15 min <= silent <= 1 day < dead.

    A missing status (``age_sec is None``) is "silent", not "dead": absence is
    not proof the agent has been gone for over a day (UNKNOWN over false alarm).
    """
    if age_sec is None:
        return "silent"
    if age_sec > _DEAD_AFTER_SEC:
        return "dead"
    if age_sec >= _SILENT_AFTER_SEC:
        return "silent"
    return "alive"


def icon_state(
    cert_level: CertLevel,
    *,
    agent_age_sec: Optional[float],
    link_age_sec: Optional[float],
) -> IconState:
    """Worst-of the cert state, agent freshness and link age (spec §2 table)."""
    fresh = agent_freshness(agent_age_sec)
    if cert_level == "alert" or fresh == "dead":
        return "alert"
    no_link = link_age_sec is not None and link_age_sec > _LINK_WARN_SEC
    if cert_level == "warn" or fresh == "silent" or no_link:
        return "warn"
    return "ok"


# --------------------------------------------------------------------------- #
# tooltip + support clipboard (spec §2)
# --------------------------------------------------------------------------- #

_ICON_PHRASE = {
    "ok": "всё в порядке",
    "warn": "требует внимания",
    "alert": "нужно действие",
}


def tooltip(view: StatusView, icon: IconState) -> str:
    ip = view.ips[0] if view.ips else "—"
    return f"{view.hostname} · {ip} · {_ICON_PHRASE.get(icon, '')}"


def support_clipboard(view: StatusView, *, cert_text: str = "—", ip: Optional[str] = None) -> str:
    """One line a user can paste to the helpdesk (spec §2 button).

    *ip* lets the panel pass its live-discovered address; defaults to the
    agent's last-written IP.
    """
    if ip is None:
        ip = view.ips[0] if view.ips else "—"
    return (
        f"ПК: {view.hostname} · IP: {ip} · "
        f"Орг/отдел: {view.org_code or '—'}/{view.dept_code or '—'} · "
        f"Сертификат: {cert_text} · Агент: {view.agent_version}"
    )


# --------------------------------------------------------------------------- #
# panel row text (spec §2; pure -> tested in test_tray_panel_logic)
# --------------------------------------------------------------------------- #


def fmt_org_dept(view: StatusView) -> str:
    return f"Организация {view.org_code or '—'} · отдел {view.dept_code or '—'}"


def fmt_ip(view: StatusView) -> str:
    return view.ips[0] if view.ips else "—"


def fmt_print(view: StatusView) -> str:
    mode = {"events": "журнал", "counter": "счётчик"}.get(view.print_mode, view.print_mode)
    return f"сегодня {view.print_today} стр. · за месяц {view.print_month} стр. ({mode})"


def fmt_link(view: StatusView, *, now: float) -> str:
    ok_ts = view.last_send_ok_ts
    if ok_ts is None:
        return "ещё не было успешной отправки"
    clock = time.strftime("%H:%M", time.localtime(ok_ts))
    if now - ok_ts > _LINK_WARN_SEC:
        return f"нет связи с сервером с {clock}"
    return f"агент работает, отправка {clock} ✓"


def fmt_disk(view: StatusView) -> tuple[str, bool]:
    """(text, warn?) -- warn when free space is low (spec §2 row 6)."""
    gb = view.disk_free_gb
    if gb is None:
        return ("неизвестно", False)
    return (f"свободно {gb:g} ГБ", gb < _DISK_WARN_GB)


def fmt_uptime(view: StatusView) -> tuple[str, bool]:
    """(text, hint?) -- soft "reboot" hint past 14 days (spec §2 row 7)."""
    days = view.uptime_days
    if days is None:
        return ("неизвестно", False)
    return (f"{days:g} дн.", days > _UPTIME_HINT_DAYS)


# --------------------------------------------------------------------------- #
# password gate (spec §4): verify + 3x/5-min lockout, persisted
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GateState:
    failed: int = 0
    locked_until: float = 0.0


def is_locked(gate: GateState, *, now: float) -> Optional[float]:
    """Seconds of lockout remaining, or None if not locked."""
    remaining = gate.locked_until - now
    return remaining if remaining > 0 else None


def check_password(
    stored_hash: str,
    attempt: str,
    gate: GateState,
    *,
    now: float,
) -> tuple[bool, GateState]:
    """Verify *attempt*; advance the lockout state machine.

    While locked even a correct password is refused. A correct password resets
    the gate; the 3rd consecutive wrong one starts a 5-minute lockout.
    """
    if is_locked(gate, now=now) is not None:
        return False, gate
    if verify_password(attempt, stored_hash):
        return True, GateState()
    failed = gate.failed + 1
    if failed >= _LOCKOUT_FAILS:
        return False, GateState(failed=0, locked_until=now + _LOCKOUT_SEC)
    return False, GateState(failed=failed, locked_until=0.0)


# --------------------------------------------------------------------------- #
# tray_state.json (gate + cert-nag share the file; never clobber the other key)
# --------------------------------------------------------------------------- #


def load_tray_state(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def save_tray_state(path: Path, state: dict[str, Any]) -> None:
    """Atomic write; an I/O failure is logged, never raised."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("tray_state.json not written (%s): %s", path, exc)


def load_gate(path: Path) -> GateState:
    blob = load_tray_state(path).get(_GATE_KEY, {})
    if not isinstance(blob, dict):
        return GateState()
    try:
        return GateState(
            failed=int(blob.get("failed", 0)),
            locked_until=float(blob.get("locked_until", 0.0)),
        )
    except (TypeError, ValueError):
        return GateState()


def save_gate(path: Path, gate: GateState) -> None:
    state = load_tray_state(path)
    state[_GATE_KEY] = {"failed": gate.failed, "locked_until": gate.locked_until}
    save_tray_state(path, state)


# --------------------------------------------------------------------------- #
# single instance (spec §2: named mutex)
# --------------------------------------------------------------------------- #


def single_instance_should_exit(create_mutex_last_error: int) -> bool:
    """True when CreateMutexW reports another instance already owns the mutex."""
    return create_mutex_last_error == _ERROR_ALREADY_EXISTS
