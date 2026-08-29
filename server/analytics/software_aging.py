"""ssd3 Ф4 software-aging engine: a PURE Resilience mechanism (K2).

A running process leaking handles or committing memory is not a Damage
mechanism -- a reboot returns the resource in full, so nothing about the
machine has changed irreversibly. That is the textbook demonstration of K2
(state != behaviour): this engine therefore never contributes a Damage
coordinate, only Resilience-loss (``risk_exposure``-style: higher = worse).

Only the CURRENT session (since the last reboot) can speak to *current*
resilience -- history from a session that already ended and was cured by a
reboot must not bleed into today's verdict (a fresh session with few points
is honestly UNKNOWN, not "inherits yesterday's leak"). Sessions are found by
walking ``heartbeat_series`` chronologically and cutting a new one wherever
``uptime_hours`` drops (a reboot). The robust per-day slope primitive lives in
``trends.theil_sen_slope`` (single source of truth for d/dt, §1.6); this
engine only reprojects it onto an hourly unit and a (uptime_hours, value) axis
instead of trends.py's (received_at, value) one.

Output is the ``software_aging_risk`` axis in the W0.5 Score100 envelope
(higher = worse): untrusted identity withholds; fewer than 4 points in the
CURRENT session -> UNKNOWN (never a confident zero from a session too young to
judge).
"""

from __future__ import annotations

import math
from typing import Any, Optional

from server.analytics.trends import theil_sen_slope
from server.scoring.score100 import (
    Direction,
    Factor,
    Score100,
    ScoreConfidence,
    band_for_risk_score,
    make_score100,
)

_MIN_SESSION_POINTS = 4
# Mirrors trends.py's own _HIGH_CONF_POINTS convention (each engine sets its own).
_HIGH_CONF_POINTS = 6

_HANDLES_WATCH = 100.0  # handles/hour -> +25 "рост"
_HANDLES_SEVERE = 300.0  # handles/hour -> +45 "утечка"
_MEM_WATCH = -50.0  # MB/hour (falling) -> +20
_UPTIME_TWO_WEEKS_H = 336.0  # 14 days -> +10
_HOURS_PER_DAY = 24.0

_UNTRUSTED_REASON = "идентификатор устройства не подтверждён (контракт §7)"
_NO_DAMAGE_NOTE = (
    "программное старение не переносится в Damage — перезагрузка возвращает ресурс "
    "полностью (К2); эта ось несёт только Resilience"
)


def _num(row: dict[str, Any], key: str) -> Optional[float]:
    v = row.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _split_sessions(hb_series: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Oldest-first sessions; a boundary is an ``uptime_hours`` DROP vs. the
    previous row. ``hb_series`` arrives newest-first (db.get_recent_heartbeats);
    rows missing uptime_hours are skipped -- they neither extend nor cut a
    session, they simply carry no session-membership evidence.
    """
    sessions: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    prev_uptime: Optional[float] = None
    for row in reversed(hb_series):
        uptime = _num(row, "uptime_hours")
        if uptime is None:
            continue
        if prev_uptime is not None and uptime < prev_uptime:
            if current:
                sessions.append(current)
            current = []
        current.append(row)
        prev_uptime = uptime
    if current:
        sessions.append(current)
    return sessions


def _session_points(session: list[dict[str, Any]], field: str) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for row in session:
        uptime = _num(row, "uptime_hours")
        value = _num(row, field)
        if uptime is not None and value is not None:
            points.append((uptime * 3600.0, value))
    return points


def _hourly_slope(session: list[dict[str, Any]], field: str) -> Optional[float]:
    """Theil-Sen slope per HOUR for one field over one session.

    Reuses trends.theil_sen_slope (the only source of d/dt, §1.6), which
    returns value-per-DAY over (epoch_seconds, value) pairs; we feed it
    uptime-in-seconds and rescale, rather than duplicating the median-of-
    pairwise-slopes primitive for a different unit.
    """
    points = _session_points(session, field)
    if len(points) < _MIN_SESSION_POINTS:
        return None
    slope_per_day = theil_sen_slope(points)
    return None if slope_per_day is None else slope_per_day / _HOURS_PER_DAY


def _percentile(values: list[float], p: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, math.ceil(p * len(ordered)) - 1))
    return ordered[idx]


def _pagefile_confirms_pressure(session: list[dict[str, Any]]) -> bool:
    """p95 pagefile_pct grew from the session's first half to its second --
    confirmation that a leak is translating into real paging pressure, not
    just a rising counter nobody feels yet."""
    vals = [v for v in (_num(r, "pagefile_pct") for r in session) if v is not None]
    if len(vals) < _MIN_SESSION_POINTS:
        return False
    mid = len(vals) // 2
    first_p95 = _percentile(vals[:mid], 0.95)
    second_p95 = _percentile(vals[mid:], 0.95)
    return first_p95 is not None and second_p95 is not None and second_p95 > first_p95


def _prev_session_leaked_field(prev_session: Optional[list[dict[str, Any]]]) -> Optional[str]:
    """Which field (if any) showed a leak-rate in the PREVIOUS session."""
    if prev_session is None:
        return None
    h = _hourly_slope(prev_session, "handle_count_total")
    if h is not None and h > _HANDLES_WATCH:
        return "handle_count_total"
    m = _hourly_slope(prev_session, "mem_avail_mb")
    if m is not None and m < _MEM_WATCH:
        return "mem_avail_mb"
    return None


def _reboot_restores(
    prev_session: Optional[list[dict[str, Any]]], last_session: list[dict[str, Any]]
) -> bool:
    """The previous session ended mid-leak, and the new session's first
    reading already looks healthier on that same field -- the resource came
    back, so the cause was software (a leak), not hardware (K2 in action)."""
    leaked = _prev_session_leaked_field(prev_session)
    if leaked is None or prev_session is None:
        return False
    prev_last = _num(prev_session[-1], leaked)
    new_first = _num(last_session[0], leaked)
    if prev_last is None or new_first is None:
        return False
    if leaked == "handle_count_total":
        return new_first < prev_last
    return new_first > prev_last


def compute_software_aging_risk(
    hb_series: list[dict[str, Any]],
    *,
    device_trust: str = "ok",
) -> Score100:
    """Deterministic software-aging (handle/memory leak) risk for one device.

    Higher = worse. Gating mirrors the other W4.2 engines: untrusted identity
    withholds entirely; fewer than ``_MIN_SESSION_POINTS`` readings in the
    CURRENT session (since the last reboot) -> UNKNOWN -- a session too young
    to judge must not read as either healthy or leaking.
    """
    direction: Direction = "higher_is_worse"

    if device_trust == "untrusted":
        return make_score100(
            None,
            direction,
            "unknown",
            "unknown",
            missing_evidence=["идентификация не подтверждена"],
            source_lineage={"identity": "untrusted"},
            reason=_UNTRUSTED_REASON,
        )

    sessions = _split_sessions(hb_series)
    last_session = sessions[-1] if sessions else []
    if len(last_session) < _MIN_SESSION_POINTS:
        return make_score100(
            None,
            direction,
            "unknown",
            "unknown",
            missing_evidence=[
                f"текущая сессия короче {_MIN_SESSION_POINTS} показаний с последней перезагрузки"
            ],
            reason=(
                "недостаточно истории текущей сессии (UNKNOWN — ложная уверенность недопустима)"
            ),
        )
    prev_session = sessions[-2] if len(sessions) >= 2 else None

    factors: list[Factor] = []
    flags: list[str] = []
    value = 0.0

    def hit(delta: float, label: str) -> None:
        nonlocal value
        value += delta
        factors.append({"label": label, "delta": round(delta, 1)})

    h_slope = _hourly_slope(last_session, "handle_count_total")
    if h_slope is not None:
        if h_slope > _HANDLES_SEVERE:
            hit(45.0, f"утечка дескрипторов (+{h_slope:.0f}/ч)")
        elif h_slope > _HANDLES_WATCH:
            hit(25.0, f"рост дескрипторов (+{h_slope:.0f}/ч)")

    m_slope = _hourly_slope(last_session, "mem_avail_mb")
    if m_slope is not None and m_slope < _MEM_WATCH:
        hit(20.0, f"снижение свободной памяти ({m_slope:.0f} МБ/ч)")

    current_uptime = _num(last_session[-1], "uptime_hours")
    if current_uptime is not None and current_uptime > _UPTIME_TWO_WEEKS_H:
        hit(10.0, f"{current_uptime / _HOURS_PER_DAY:.0f} дн. без перезагрузки")

    if _pagefile_confirms_pressure(last_session):
        hit(10.0, "рост pagefile во второй половине сессии — подтверждение утечки")

    if h_slope is not None and h_slope > 0 and prev_session is not None:
        prev_h_slope = _hourly_slope(prev_session, "handle_count_total")
        if prev_h_slope is not None and prev_h_slope > 0 and h_slope >= 2 * prev_h_slope:
            hit(10.0, "утечка дескрипторов ускоряется от сессии к сессии")
            flags.append("aging_accelerating")

    if _reboot_restores(prev_session, last_session):
        factors.append(
            {"label": "перезагрузка возвращает ресурс — утечка программная", "delta": 0.0}
        )
        flags.append("reboot_restores")

    value = max(0.0, min(100.0, value))
    band = band_for_risk_score(value)
    # T4.1/K8: band watch/bad is the ratchet-facing signal for Ф6, not any one factor.
    if band in ("watch", "bad"):
        flags.append("aging_leak")

    confidence: ScoreConfidence = "high" if len(last_session) >= _HIGH_CONF_POINTS else "medium"

    missing = [_NO_DAMAGE_NOTE]
    if h_slope is None:
        missing.append("наклон дескрипторов не считается (мало точек в сессии)")
    if m_slope is None:
        missing.append("наклон свободной памяти не считается (мало точек в сессии)")

    return make_score100(
        value,
        direction,
        band,
        confidence,
        factors=factors,
        missing_evidence=missing,
        source_lineage={
            "session_points": len(last_session),
            "sessions_seen": len(sessions),
            "handles_slope_per_h": round(h_slope, 1) if h_slope is not None else None,
            "mem_slope_per_h": round(m_slope, 1) if m_slope is not None else None,
            "coords": {"flags": flags},
        },
        reason="" if value > 0.0 else "сигналов программного старения не обнаружено",
    )
