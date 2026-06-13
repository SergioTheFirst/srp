"""Pure-logic tests for the tray state layer (tray spec §2 + §4).

The tray itself is a thin ctypes/tkinter adapter; everything decidable lives in
``client.tray.state`` as pure functions so it is testable off-Windows. These tests
pin the icon worst-of matrix, agent/link freshness thresholds, the support
clipboard string, and the password gate (verify + 3x/5-min lockout, persisted in
tray_state.json without clobbering the cert-nag keys).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from client.config import hash_password
from client.tray import state as st

# --------------------------------------------------------------------------- #
# status.json reading + freshness
# --------------------------------------------------------------------------- #

_DOC = {
    "ts": 1_000_000,
    "agent_version": "0.3.0",
    "last_send_ok_ts": 999_400,
    "last_send_error": "",
    "buffer_depth": 0,
    "hostname": "PC-042",
    "ips": ["192.168.1.42"],
    "org_code": "101",
    "dept_code": "7",
    "print_today_pages": 14,
    "print_month_pages": 312,
    "print_mode": "events",
    "disk_free_gb": 41.2,
    "uptime_days": 3.5,
}


def _write(path: Path, doc: dict) -> Path:
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_read_status_parses_and_computes_ages(tmp_path: Path) -> None:
    p = _write(tmp_path / "status.json", _DOC)
    view = st.read_status(p, now=1_000_060)  # 60 s after ts
    assert view is not None
    assert view.hostname == "PC-042"
    assert view.ips == ["192.168.1.42"]
    assert view.org_code == "101" and view.dept_code == "7"
    assert view.age_sec == pytest.approx(60)
    assert view.link_age_sec == pytest.approx(1_000_060 - 999_400)


def test_read_status_missing_or_broken_returns_none(tmp_path: Path) -> None:
    assert st.read_status(tmp_path / "absent.json", now=1) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert st.read_status(bad, now=1) is None


def test_read_status_never_sent_has_no_link_age(tmp_path: Path) -> None:
    doc = {**_DOC, "last_send_ok_ts": None}
    p = _write(tmp_path / "status.json", doc)
    view = st.read_status(p, now=1_000_060)
    assert view is not None
    assert view.link_age_sec is None


def test_agent_freshness_thresholds() -> None:
    assert st.agent_freshness(0) == "alive"
    assert st.agent_freshness(899) == "alive"
    assert st.agent_freshness(900) == "silent"  # 15 min
    assert st.agent_freshness(86_400) == "silent"
    assert st.agent_freshness(86_401) == "dead"  # > 1 day
    # Missing status (no file) -> we cannot confirm a >1-day death: warn, not red.
    assert st.agent_freshness(None) == "silent"


# --------------------------------------------------------------------------- #
# icon worst-of matrix (spec §2)
# --------------------------------------------------------------------------- #


def test_icon_state_all_ok() -> None:
    assert st.icon_state("ok", agent_age_sec=10, link_age_sec=30) == "ok"
    assert st.icon_state("unknown", agent_age_sec=10, link_age_sec=30) == "ok"


def test_icon_state_warn_paths() -> None:
    # cert warn alone
    assert st.icon_state("warn", agent_age_sec=10, link_age_sec=10) == "warn"
    # no link > 1h alone
    assert st.icon_state("ok", agent_age_sec=10, link_age_sec=3_601) == "warn"
    # agent silent (>=15 min, <=1 day) alone
    assert st.icon_state("ok", agent_age_sec=1_000, link_age_sec=10) == "warn"


def test_icon_state_alert_paths_beat_warn() -> None:
    # cert alert wins even if everything else is fine
    assert st.icon_state("alert", agent_age_sec=10, link_age_sec=10) == "alert"
    # agent dead (>1 day) wins
    assert st.icon_state("ok", agent_age_sec=90_000, link_age_sec=10) == "alert"
    # alert dominates a simultaneous warn
    assert st.icon_state("alert", agent_age_sec=1_000, link_age_sec=3_601) == "alert"


# --------------------------------------------------------------------------- #
# support clipboard + tooltip
# --------------------------------------------------------------------------- #


def test_support_clipboard_one_line_has_key_facts(tmp_path: Path) -> None:
    view = st.read_status(_write(tmp_path / "s.json", _DOC), now=1_000_060)
    line = st.support_clipboard(view, cert_text="действителен до 24.06.2026")
    assert "PC-042" in line
    assert "192.168.1.42" in line
    assert "101" in line and "7" in line
    assert "0.3.0" in line
    assert "\n" not in line  # single line for the clipboard


def test_tooltip_reflects_icon_phrase(tmp_path: Path) -> None:
    view = st.read_status(_write(tmp_path / "s.json", _DOC), now=1_000_060)
    assert "PC-042" in st.tooltip(view, "ok")
    assert st.tooltip(view, "ok") != st.tooltip(view, "alert")


# --------------------------------------------------------------------------- #
# password gate: verify + lockout (spec §4)
# --------------------------------------------------------------------------- #


def test_correct_password_unlocks_and_resets() -> None:
    h = hash_password("s3cret")
    ok, gate = st.check_password(h, "s3cret", st.GateState(failed=2), now=100.0)
    assert ok is True
    assert gate.failed == 0 and gate.locked_until == 0.0


def test_three_wrong_locks_for_five_minutes() -> None:
    h = hash_password("s3cret")
    gate = st.GateState()
    ok, gate = st.check_password(h, "x", gate, now=100.0)
    assert ok is False and gate.failed == 1
    ok, gate = st.check_password(h, "x", gate, now=101.0)
    assert ok is False and gate.failed == 2
    ok, gate = st.check_password(h, "x", gate, now=102.0)
    assert ok is False
    # locked: counter reset, locked_until set 5 min out
    assert st.is_locked(gate, now=102.0) == pytest.approx(300.0)
    assert st.is_locked(gate, now=402.1) is None  # expired


def test_attempts_ignored_while_locked() -> None:
    h = hash_password("s3cret")
    locked = st.GateState(failed=0, locked_until=500.0)
    # even the CORRECT password is refused while the lockout window is open
    ok, gate = st.check_password(h, "s3cret", locked, now=400.0)
    assert ok is False
    assert gate.locked_until == 500.0  # unchanged


def test_gate_state_persists_without_clobbering_cert_keys(tmp_path: Path) -> None:
    p = tmp_path / "tray_state.json"
    # a pre-existing cert-nag entry (stage 5 territory) must survive a gate write
    p.write_text(json.dumps({"abc123": {"last_nag_date": "2026-06-13"}}), encoding="utf-8")
    st.save_gate(p, st.GateState(failed=2, locked_until=777.0))
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["abc123"] == {"last_nag_date": "2026-06-13"}  # untouched
    loaded = st.load_gate(p)
    assert loaded.failed == 2 and loaded.locked_until == 777.0


def test_load_gate_missing_or_broken_is_clean() -> None:
    assert st.load_gate(Path("does-not-exist.json")) == st.GateState()


# --------------------------------------------------------------------------- #
# single instance (spec §2: named mutex)
# --------------------------------------------------------------------------- #


def test_single_instance_exits_only_on_already_exists() -> None:
    assert st.single_instance_should_exit(183) is True  # ERROR_ALREADY_EXISTS
    assert st.single_instance_should_exit(0) is False
