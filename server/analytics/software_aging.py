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
(higher = worse): untrusted identity withholds; fewer than 4 usable
(post-logon, post-warm-up) points in the CURRENT session -> UNKNOWN (never a
confident zero from a session too young to judge). A leak is flagged only on a
significant slope over >= ``_MIN_SPAN_H`` of usable rows (calibration D2).
"""

from __future__ import annotations

import math
from typing import Any, Optional

from server.analytics.trends import _MK_Z_CRIT, mann_kendall_z, theil_sen_slope
from server.scoring.score100 import (
    Direction,
    Factor,
    Score100,
    ScoreConfidence,
    band_for_risk_score,
    make_score100,
)

_MIN_SESSION_POINTS = 4
# Each engine sets its own confidence depth (trends.py now gates on span instead).
_HIGH_CONF_POINTS = 6
# Post-logon application ramp: one 4 h agent cadence step.
_WARMUP_H = 4.0
# Calibration knob: the F13 Monte Carlo verified 72 h; 48 h keeps 3-day reboot
# cycles assessable. With the warm-up drop and Mann-Kendall the design MC saw 0
# false flags in 11 400 evaluations of stationary handles (sigma 700) and caught
# >= 150 handles/h at the first evaluation after 52 h of uptime.
_MIN_SPAN_H = 48.0

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


def _usable_rows(session: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The rows that can speak to a leak.

    (i) A session that saw a logged-on user drops its ``user_present`` False rows:
    the pre-logon boot heartbeat and Fast-Startup resume heartbeats
    (client/collectors/heartbeat.py: user_present = explorer.exe running). None
    (old agents) stays; with no True row at all (kiosk / service PC) every row
    stays. (ii) Rows under ``_WARMUP_H`` of uptime are the post-logon ramp.
    """
    logon = any(r.get("user_present") is True for r in session)
    return [
        r
        for r in session
        if not (logon and r.get("user_present") is False)
        and (_num(r, "uptime_hours") or 0.0) >= _WARMUP_H
    ]


def _session_points(session: list[dict[str, Any]], field: str) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for row in session:
        uptime = _num(row, "uptime_hours")
        value = _num(row, field)
        if uptime is not None and value is not None:
            points.append((uptime * 3600.0, value))
    return points


def _assessable(points: list[tuple[float, float]]) -> bool:
    """Enough usable readings over enough uptime for a slope to mean anything."""
    if len(points) < _MIN_SESSION_POINTS:
        return False
    return points[-1][0] - points[0][0] >= _MIN_SPAN_H * 3600.0


def _hourly_slope(session: list[dict[str, Any]], field: str) -> Optional[float]:
    """Significant Theil-Sen slope per HOUR for one field over one usable session.

    Reuses trends.theil_sen_slope (the only source of d/dt, §1.6), which
    returns value-per-DAY over (epoch_seconds, value) pairs; we feed it
    uptime-in-seconds and rescale, rather than duplicating the median-of-
    pairwise-slopes primitive for a different unit. None unless the session is
    assessable and Mann-Kendall agrees (|z| >= 1.96, same sign as the slope).
    """
    points = _session_points(session, field)
    if not _assessable(points):
        return None
    slope_per_day = theil_sen_slope(points)
    z = mann_kendall_z(points)
    if slope_per_day is None or abs(z) < _MK_Z_CRIT or z * slope_per_day <= 0:
        return None
    return slope_per_day / _HOURS_PER_DAY


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


def _leak_seen(h_slope: Optional[float], m_slope: Optional[float]) -> bool:
    """A real, significant leak slope -- the only thing that may flag aging."""
    handles = h_slope is not None and h_slope > _HANDLES_WATCH
    return handles or (m_slope is not None and m_slope < _MEM_WATCH)


def _accelerating(h_slope: Optional[float], prev_session: Optional[list[dict[str, Any]]]) -> bool:
    """Both sessions leak handles above watch and the current one >= 2x faster."""
    if h_slope is None or h_slope <= _HANDLES_WATCH or prev_session is None:
        return False
    prev = _hourly_slope(prev_session, "handle_count_total")
    return prev is not None and prev > _HANDLES_WATCH and h_slope >= 2 * prev


def _aging_factors(
    last_session: list[dict[str, Any]],
    prev_session: Optional[list[dict[str, Any]]],
    h_slope: Optional[float],
    m_slope: Optional[float],
) -> tuple[list[Factor], list[str]]:
    """Explainable factors (their deltas sum to the axis value) + coordinate flags."""
    factors: list[Factor] = []
    flags: list[str] = []
    if h_slope is not None and h_slope > _HANDLES_SEVERE:
        factors.append({"label": f"утечка дескрипторов (+{h_slope:.0f}/ч)", "delta": 45.0})
    elif h_slope is not None and h_slope > _HANDLES_WATCH:
        factors.append({"label": f"рост дескрипторов (+{h_slope:.0f}/ч)", "delta": 25.0})
    if m_slope is not None and m_slope < _MEM_WATCH:
        factors.append({"label": f"снижение свободной памяти ({m_slope:.0f} МБ/ч)", "delta": 20.0})
    uptime = _num(last_session[-1], "uptime_hours")
    if uptime is not None and uptime > _UPTIME_TWO_WEEKS_H:
        factors.append(
            {"label": f"{uptime / _HOURS_PER_DAY:.0f} дн. без перезагрузки", "delta": 10.0}
        )
    # Context (uptime, pagefile) only confirms a real leak -- never flags one alone.
    if _leak_seen(h_slope, m_slope) and _pagefile_confirms_pressure(last_session):
        factors.append(
            {
                "label": "рост pagefile во второй половине сессии — подтверждение утечки",
                "delta": 10.0,
            }
        )
    if _accelerating(h_slope, prev_session):
        factors.append(
            {"label": "утечка дескрипторов ускоряется от сессии к сессии", "delta": 10.0}
        )
        flags.append("aging_accelerating")
    if _reboot_restores(prev_session, last_session):
        factors.append(
            {"label": "перезагрузка возвращает ресурс — утечка программная", "delta": 0.0}
        )
        flags.append("reboot_restores")
    return factors, flags


def _missing_notes(last_session: list[dict[str, Any]]) -> list[str]:
    """What the axis cannot see. A healthy flat session is assessed, not missing."""
    missing = [_NO_DAMAGE_NOTE]
    for field, label in (
        ("handle_count_total", "дескрипторов"),
        ("mem_avail_mb", "свободной памяти"),
    ):
        if not _assessable(_session_points(last_session, field)):
            missing.append(
                f"наклон {label} не считается (мало истории сессии: нужно >= {_MIN_SPAN_H:g} ч)"
            )
    return missing


def _confidence(last_session: list[dict[str, Any]]) -> ScoreConfidence:
    """'high' needs ``_HIGH_CONF_POINTS`` usable rows AND an assessable slope: a session
    too young for either slope cannot see a leak, so its zero is never 'high'."""
    fields = ("handle_count_total", "mem_avail_mb")
    seen = any(_assessable(_session_points(last_session, f)) for f in fields)
    return "high" if seen and len(last_session) >= _HIGH_CONF_POINTS else "medium"


def _unknown(missing: str, reason: str, lineage: Optional[dict[str, Any]] = None) -> Score100:
    direction: Direction = "higher_is_worse"
    return make_score100(
        None,
        direction,
        "unknown",
        "unknown",
        missing_evidence=[missing],
        source_lineage=lineage,
        reason=reason,
    )


def compute_software_aging_risk(
    hb_series: list[dict[str, Any]],
    *,
    device_trust: str = "ok",
) -> Score100:
    """Deterministic software-aging (handle/memory leak) risk for one device.

    Higher = worse. Gating mirrors the other W4.2 engines: untrusted identity
    withholds entirely; fewer than ``_MIN_SESSION_POINTS`` usable readings in the
    CURRENT session (since the last reboot) -> UNKNOWN -- a session too young
    to judge must not read as either healthy or leaking.
    """
    if device_trust == "untrusted":
        lineage = {"identity": "untrusted"}
        return _unknown("идентификация не подтверждена", _UNTRUSTED_REASON, lineage)
    sessions = [_usable_rows(s) for s in _split_sessions(hb_series)]
    last_session = sessions[-1] if sessions else []
    if len(last_session) < _MIN_SESSION_POINTS:
        # perf_rows: heartbeats DO arrive -- health's liveness witness, since throttle
        # points count only a loaded CPU (H3) and free-space/latency may be missing
        perf_rows = sum(1 for r in hb_series if r.get("cpu_perf_pct") is not None)
        return _unknown(
            f"текущая сессия короче {_MIN_SESSION_POINTS} показаний с последней перезагрузки",
            "недостаточно истории текущей сессии (UNKNOWN — ложная уверенность недопустима)",
            {"perf_rows": perf_rows},
        )
    prev_session = sessions[-2] if len(sessions) >= 2 else None
    h_slope = _hourly_slope(last_session, "handle_count_total")
    m_slope = _hourly_slope(last_session, "mem_avail_mb")
    factors, flags = _aging_factors(last_session, prev_session, h_slope, m_slope)
    value = max(0.0, min(100.0, sum(f["delta"] for f in factors)))
    band = band_for_risk_score(value)
    # T4.1/K8: band watch/bad is the ratchet-facing signal for Ф6 -- and only a
    # real leak slope may raise it (uptime / pagefile context alone never does).
    if band in ("watch", "bad") and _leak_seen(h_slope, m_slope):
        flags.append("aging_leak")
    return make_score100(
        value,
        "higher_is_worse",
        band,
        _confidence(last_session),
        factors=factors,
        missing_evidence=_missing_notes(last_session),
        source_lineage={
            "session_points": len(last_session),
            "sessions_seen": len(sessions),
            "handles_slope_per_h": round(h_slope, 1) if h_slope is not None else None,
            "mem_slope_per_h": round(m_slope, 1) if m_slope is not None else None,
            "coords": {"flags": flags},
        },
        reason="" if value > 0.0 else "сигналов программного старения не обнаружено",
    )
