"""W4.1 deterministic trend engine: slopes + ETA over append-only history.

The first genuine early-warning value (cctodo §4.1): now that P0 gives a trusted,
server-time, append-only series per device, we can ask *"is this getting worse,
and how fast?"* purely arithmetically -- no ML, no training (this is D4).

Design rules (telemetry-trust-contract + CLAUDE §5):
  * **Robust** slope (Theil-Sen median of pairwise slopes) -- one bad reading or a
    counter reset must not invent a trend.
  * **Server time** x-axis (``received_at``, W0.2) -- never trust the client clock.
  * **UNKNOWN over false confidence** -- below ``_MIN_POINTS`` samples or too short
    a span the trend is 'insufficient'; a direction needs a significant
    Mann-Kendall z (calibration D1) plus a material slope, and an ETA needs
    ``_ETA_MIN_SPAN_H`` of history -- never a coin-flip sign of noise.
  * The aggregate ``trajectory_risk`` reuses the W0.5 Score100 envelope (same
    gating: untrusted identity withholds; insufficient data -> UNKNOWN).

No nested confidence calculus -- simple, explicit thresholds only (contract §13).
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Callable, Optional

from server.analytics.trend_extractors import (
    _boot_ms,
    _cpu_perf_pct,
    _disk_nvme_media_errors,
    _disk_nvme_spare_pct,
    _disk_smart_attr,
    _disk_tail_ratio,
    _free_space_pct,
    _gateway_latency_ms,
    _max_storage_wear,
)
from server.scoring.score100 import (
    Direction,
    Factor,
    Score100,
    ScoreConfidence,
    band_for_risk_score,
    make_score100,
)

# Below this many usable samples a slope is noise, not a trend -> UNKNOWN.
_MIN_POINTS = 6
_MK_Z_CRIT = 1.96  # Mann-Kendall critical |z|: two-sided alpha 0.05
_ETA_MIN_SPAN_H = 72.0  # an ETA from hours of history ran x40-90 past its window (D1)
_HIGH_CONF_SPAN_DAYS = 7.0  # trajectory "high" confidence needs a trend this long
# Last readings at/below a falling metric's threshold = crossed. 0.3.8 (1 h cadence):
# 16 readings ~= 16 h, as 4 did at 4 h.
_BREACH_READINGS = 16
_RUNAWAY_BLOCK = 12  # readings per ~12 h block at the 1 h cadence (0.3.8; was 3 at 4 h)
_RUNAWAY_WINDOW_H = 72.0  # disk_fill runaway candidate: Theil-Sen of this tail
_RATE_WINDOW_DAYS = 7.0  # nvme_spare ETA: consumption rate over this tail
# Practical free-space floor: disk_fill.py already grades < 8 % severe and < 3 %
# critical; 5 % is ~13 GB on a 256 GB office SSD, the order Windows cumulative /
# feature updates plus pagefile growth need.
_DISK_FILL_FLOOR_PCT = 5.0
_SECONDS_PER_DAY = 86400.0
_SECONDS_PER_HOUR = 3600.0

# ETA (days to threshold) -> trajectory risk 0..100 (higher = sooner = worse).
# Deterministic horizon bands an operator can act on, not a probability.
_ETA_RISK_BANDS = ((30.0, 90.0), (90.0, 60.0), (180.0, 35.0), (365.0, 15.0))
_ETA_RISK_FAR = 5.0  # depleting, but > 1 year out
# Rated write endurance is not failure (NVMe 2.0c Fig. 207: "100 ... may not
# indicate an NVM subsystem failure"): wear never drives trajectory past the
# 180-day band, so R stays >= 70 and wear alone never moves the state.
_WEAR_RISK_CAP = _ETA_RISK_BANDS[2][1]
# H11 (owner decision 1): a filling system drive is urgency, not device degradation --
# capped at 80: x 0.85 = 68 -> R 32 -> h2 even at ETA <= 30 or a breach (horizon 7
# «предел уже достигнут» says how soon). Only this domain: the NVMe spare keeps 90.
_FILL_RISK_CAP = 80.0
_WEAR_EXHAUSTED = "расчётный ресурс записи исчерпан (≥100%) — плановая замена, не прогноз отказа"


# --------------------------------------------------------------------------- #
# Primitives (pure arithmetic)
# --------------------------------------------------------------------------- #
def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _to_points(
    series: list[dict[str, Any]], extractor: Callable[[dict[str, Any]], Optional[float]]
) -> list[tuple[float, float]]:
    """(epoch_seconds, value) pairs, oldest-first, on server time; Nones dropped.

    ``series`` arrives newest-first from the DB; we sort ascending so the *last*
    point is the current reading. A NaN/inf reading is no reading: dropped like
    None, so it never sits "at/below" a threshold or becomes the current level.
    """
    points: list[tuple[float, float]] = []
    for row in series:
        when = _parse_iso(row.get("received_at") or row.get("ts"))
        if when is None:
            continue
        value = extractor(row)
        if value is None or not math.isfinite(value):
            continue
        points.append((when.timestamp(), float(value)))
    points.sort(key=lambda p: p[0])
    return points


def theil_sen_slope(points: list[tuple[float, float]]) -> Optional[float]:
    """Robust slope (value per DAY) = median of all pairwise slopes.

    Resistant to outliers and one-off counter resets in a way least-squares is
    not. Returns None when no two points are separated in time.
    """
    slopes: list[float] = []
    n = len(points)
    for i in range(n):
        ti, vi = points[i]
        for j in range(i + 1, n):
            tj, vj = points[j]
            dt = tj - ti
            if dt > 0:
                slopes.append((vj - vi) / dt)
    if not slopes:
        return None
    return median(slopes) * _SECONDS_PER_DAY


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def mann_kendall_z(points: list[tuple[float, float]]) -> float:
    """Tie-corrected Mann-Kendall z of time-sorted (t, value) points; 0.0 = no trend.

    S = sum over i<j of sign(v_j - v_i); var(S) drops t(t-1)(2t+5) for every
    group of t equal values; the -+1 is the continuity correction. |z| >=
    ``_MK_Z_CRIT`` is a significant monotone trend (two-sided alpha 0.05).

    ponytail: assumes independent samples -- autocorrelated series run about
    2.5-5 % false 'worsening' (design MC, AR(1) phi 0.6); the upgrade path is the
    Hamed-Rao effective-n variance correction.
    """
    values = [v for _, v in points]
    n = len(values)
    s = sum(_sign(values[j] - values[i]) for i in range(n) for j in range(i + 1, n))
    ties = sum(t * (t - 1) * (2 * t + 5) for t in Counter(values).values())
    var = (n * (n - 1) * (2 * n + 5) - ties) / 18.0
    if s == 0 or var <= 0:
        return 0.0
    return (s - _sign(s)) / math.sqrt(var)


def eta_days_to_threshold(
    current: float, slope_per_day: float, threshold: float
) -> Optional[float]:
    """Days until ``current`` reaches ``threshold`` at ``slope_per_day``.

    None when the slope is zero or pointing *away* from the threshold (i.e. the
    metric is stable or improving) -- we refuse to extrapolate a crossing that
    the data does not support.
    """
    if slope_per_day == 0.0:
        return None
    days = (threshold - current) / slope_per_day
    return days if days > 0 else None


# A step opposite the worsening direction of at least this much (in metric units)
# is a reset, not a trend: a drive swap drops wear% ~85->~5 (wear is physically
# monotonic, so any real drop is a new part), and a cleanup that frees >= 10 pp
# restarts a free-space depletion trend (Windows Update staging rebounds are
# only 2-8 pp). We anchor on the most recent segment rather than averaging across it.
_RESET_STEP = 10.0


def _anchor_recent_segment(
    points: list[tuple[float, float]], worsening_sign: int, reset_step: float
) -> list[tuple[float, float]]:
    """Keep only the points after the last reset (part replacement / cleanup).

    Theil-Sen survives a single outlier but not a sustained level shift; for a
    metric that only moves one way in normal life (wear, a filling disk) a move
    the *other* way of ``reset_step`` or more starts a new series.
    """
    for i in range(len(points) - 1, 0, -1):
        step = points[i][1] - points[i - 1][1]
        if step * worsening_sign <= -reset_step:
            return points[i:]
    return points


_Points = list[tuple[float, float]]


def _span_h(points: _Points) -> float:
    return (points[-1][0] - points[0][0]) / _SECONDS_PER_HOUR if points else 0.0


def _span_days(points: _Points) -> Optional[float]:
    return _span_h(points) / 24.0 if len(points) >= 2 else None


def _tail(points: _Points, hours: float) -> _Points:
    """The points younger than ``hours`` before the newest one (newest included)."""
    start = points[-1][0] - hours * _SECONDS_PER_HOUR
    return [p for p in points if p[0] > start]


def _steepest(candidates: list[Optional[float]], sign: int) -> Optional[float]:
    """The steepest nonzero candidate pointing ``sign``; None when none qualifies."""
    usable = [c for c in candidates if c is not None and c != 0 and _sign(c) == sign]
    return max(usable, key=abs, default=None)


# --------------------------------------------------------------------------- #
# Per-metric trend
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TrendResult:
    metric: str
    n_points: int
    current: Optional[float]
    slope_per_day: Optional[float]  # the slope behind direction / ETA; None on a level breach
    eta_days: Optional[float]
    target_date: Optional[str]  # ISO date of the projected crossing, if any
    direction: str  # "worsening" | "improving" | "flat" | "insufficient"
    reason: str = ""
    # ssd3 Ф2 (Resilience dynamics, K4): slope over the second half of the
    # window + whether it has (at least) doubled -- both None while the trend
    # is 'insufficient' (and on a level breach), so a thin history never
    # fabricates "accelerating".
    slope_recent: Optional[float] = None
    accelerating: Optional[bool] = None
    span_days: Optional[float] = None  # first..last reading; None below 2 readings


def trend_to_dict(t: TrendResult) -> dict[str, Any]:
    return {
        "metric": t.metric,
        "n_points": t.n_points,
        "current": t.current,
        "slope_per_day": t.slope_per_day,
        "eta_days": t.eta_days,
        "target_date": t.target_date,
        "direction": t.direction,
        "reason": t.reason,
        "slope_recent": t.slope_recent,
        "accelerating": t.accelerating,
        "span_days": t.span_days,
    }


_DIRECTIONS = {1: "worsening", -1: "improving", 0: "flat"}
# One rule's verdict: (direction, slope_per_day, eta_days, reason).
_Verdict = tuple[str, Optional[float], Optional[float], str]


def _target_date(eta: Optional[float], now: Optional[datetime]) -> Optional[str]:
    if eta is None:
        return None
    base = now or datetime.now(timezone.utc)
    return (base + timedelta(days=eta)).date().isoformat()


def _insufficient(metric: str, points: _Points, reason: str) -> TrendResult:
    current, span = (points[-1][1] if points else None), _span_days(points)
    return TrendResult(
        metric, len(points), current, None, None, None, "insufficient", reason, span_days=span
    )


def _gate_reason(points: _Points, slope: Optional[float], min_span_h: Optional[float]) -> str:
    """Why the series cannot carry a direction yet ('' = it can)."""
    if len(points) < _MIN_POINTS:
        return f"нужно >= {_MIN_POINTS} показаний, есть {len(points)}"
    if slope is None:
        return "показания не разделены во времени"
    if min_span_h is not None and _span_h(points) < min_span_h:
        return f"мало истории: нужно >= {min_span_h:g} ч наблюдений"
    return ""


def _level_breach(
    metric: str,
    points: _Points,
    worsening_sign: int,
    threshold: Optional[float],
    now: Optional[datetime],
) -> Optional[TrendResult]:
    """A falling metric whose last ``_BREACH_READINGS`` readings all sit at/below its
    threshold has crossed: ETA 0 before any n/span gate -- a sudden fill or a spare
    collapse below the vendor floor must not wait 6 readings / 72 h. The level, not
    a slope, sets the direction, so slope_per_day is None: a disk recovering under
    the floor has an improving window slope that would contradict 'worsening'."""
    if worsening_sign >= 0 or threshold is None or len(points) < _BREACH_READINGS:
        return None
    if not all(v <= threshold for _, v in points[-_BREACH_READINGS:]):
        return None
    reason = f"последние {_BREACH_READINGS} показания за порогом {threshold:g}"
    today, span = _target_date(0.0, now), _span_days(points)
    return TrendResult(
        metric, len(points), points[-1][1], None, 0.0, today, "worsening", reason, span_days=span
    )


def _eta(
    points: _Points,
    eff: Optional[float],
    threshold: Optional[float],
    worsening_sign: int,
    level_readings: int = 1,
) -> Optional[float]:
    """Days to ``threshold`` along ``eff``, only on >= ``_ETA_MIN_SPAN_H`` of history.
    Only a FALLING metric can already sit past its boundary (ETA 0): a rising one
    past it (wear > 100 %) has no crossing left to project."""
    if threshold is None or eff is None or _span_h(points) < _ETA_MIN_SPAN_H:
        return None
    # H7: the level is the median of the last readings -- one stray low reading of a
    # gauge must not move its forecast to "reached" (1 = the last reading, as before)
    current = median(v for _, v in points[-level_readings:])
    if worsening_sign < 0 and current <= threshold:
        return 0.0
    return eta_days_to_threshold(current, eff, threshold)


def _rate_slope(points: _Points, z: float, slope: float) -> Optional[float]:
    """Counter ETA slope: the steepest z-agreeing of the window Theil-Sen, the
    ``_RATE_WINDOW_DAYS`` rate and the window-average rate (an integer spare %
    moves in steps whose Theil-Sen medians are 0)."""
    t_end, v_end = points[-1]
    t_rate, v_rate = _tail(points, _RATE_WINDOW_DAYS * 24.0)[0]
    rate_days = (t_end - t_rate) / _SECONDS_PER_DAY
    rate = (v_end - v_rate) / rate_days if rate_days >= 1.0 else None
    window = (v_end - points[0][1]) * 24.0 / _span_h(points)
    return _steepest([slope, rate, window], _sign(z))


def _counter_rule(
    points: _Points, slope: float, worsening_sign: int, threshold: Optional[float]
) -> _Verdict:
    """Damage counters and NVMe spare take the sign of a significant z: a pure step
    has a Theil-Sen of 0 yet is a genuine change; 0<->1 toggles and a return to an
    earlier maximum are not significant. slope_per_day stays the window Theil-Sen
    (it may be 0 while 'worsening') unless an ETA was projected."""
    z = mann_kendall_z(points)
    dir_sign = _sign(z) if abs(z) >= _MK_Z_CRIT else 0
    direction = _DIRECTIONS[dir_sign * worsening_sign]
    worse = direction == "worsening" and threshold is not None
    eff = _rate_slope(points, z, slope) if worse else None
    eta = _eta(points, eff, threshold, worsening_sign)
    return direction, (eff if eta is not None else slope), eta, ""


def _runaway_slope(points: _Points, z: float, worsening_sign: int, floor: float) -> Optional[float]:
    """disk_fill runaway candidate: the Theil-Sen of the last ``_RUNAWAY_WINDOW_H``
    (>= ``_MIN_POINTS`` readings) worsening by >= ``floor``/day, admitted only when
    the last 36 readings -- three consecutive 12-reading blocks, ~12 h each at the
    1 h cadence (0.3.8) -- drop twice in a row by >= floor/2: a single copy step
    moves one block boundary."""
    if z * worsening_sign <= 0 or len(points) < 3 * _RUNAWAY_BLOCK:
        return None
    window = _tail(points, _RUNAWAY_WINDOW_H)
    run = theil_sen_slope(window) if len(window) >= _MIN_POINTS else None
    if run is None or run * worsening_sign < floor:
        return None
    tail = [v for _, v in points[-3 * _RUNAWAY_BLOCK :]]
    m1, m2, m3 = (
        median(tail[i : i + _RUNAWAY_BLOCK]) for i in range(0, 3 * _RUNAWAY_BLOCK, _RUNAWAY_BLOCK)
    )
    if min((m2 - m1) * worsening_sign, (m3 - m2) * worsening_sign) < floor / 2.0:
        return None
    return run


def _effective_slope(
    points: _Points,
    slope: float,
    recent: Optional[float],
    worsening_sign: int,
    runaway_floor: Optional[float],
) -> Optional[float]:
    """Steepest candidate agreeing with a significant z (None when z is not): the
    window Theil-Sen; the recent half only when it spans >= ``_ETA_MIN_SPAN_H`` and
    is significant itself (without that gate 'steepest of two' gave S9, a slow
    -0.1 pp/day with noise, ETA <= 30 in 4/40 seeds; design variant 7); with
    ``runaway_floor``, the runaway slope."""
    z = mann_kendall_z(points)
    if abs(z) < _MK_Z_CRIT:
        return None
    candidates: list[Optional[float]] = [slope]
    half = points[len(points) // 2 :]
    if _span_h(half) >= _ETA_MIN_SPAN_H:
        z_half = mann_kendall_z(half)
        if abs(z_half) >= _MK_Z_CRIT and _sign(z_half) == _sign(z):
            candidates.append(recent)
    if runaway_floor is not None:
        candidates.append(_runaway_slope(points, z, worsening_sign, runaway_floor))
    return _steepest(candidates, _sign(z))


def _material(eff: float, current: float, floor_abs: Optional[float], floor_rel: float) -> bool:
    """The 30-day drift clears ``floor_abs`` units, else ``floor_rel`` of the level."""
    drift = abs(eff) * 30.0
    if floor_abs is not None:
        return drift >= floor_abs
    return current == 0 or drift >= floor_rel * abs(current)


def _gauge_rule(
    points: _Points,
    slope: float,
    eff: Optional[float],
    worsening_sign: int,
    threshold: Optional[float],
    min_distinct: int,
    level_readings: int = 1,
) -> _Verdict:
    """A gauge moves only along a material effective slope ``eff`` (None = none). Too
    few distinct values read 'flat', not 'insufficient': months of constant wear
    are known-stable."""
    if len({v for _, v in points}) < min_distinct:
        return "flat", slope, None, f"мало шагов значения: нужно >= {min_distinct} разных значений"
    if eff is None:
        return "flat", slope, None, ""
    direction = _DIRECTIONS[_sign(eff) * worsening_sign]
    worse = direction == "worsening"
    eta = _eta(points, eff, threshold, worsening_sign, level_readings) if worse else None
    return direction, eff, eta, ""


def _accelerating(direction: str, slope: float, recent: Optional[float], ws: int) -> bool:
    """The recent half moves >= 2x a worsening window slope (``ws`` = worsening sign).
    A pure step has a window Theil-Sen of 0, so it never reads as acceleration."""
    if direction != "worsening" or recent is None:
        return False
    return slope * ws > 0 and recent * ws >= 2 * slope * ws


def _result(
    metric: str,
    points: _Points,
    verdict: _Verdict,
    slope: float,
    recent: Optional[float],
    worsening_sign: int,
    now: Optional[datetime],
) -> TrendResult:
    direction, slope_per_day, eta, reason = verdict
    return TrendResult(
        metric,
        len(points),
        points[-1][1],
        slope_per_day,
        eta,
        _target_date(eta, now),
        direction,
        reason,
        slope_recent=recent,
        accelerating=_accelerating(direction, slope, recent, worsening_sign),
        span_days=_span_days(points),
    )


def build_trend(
    series: list[dict[str, Any]],
    metric: str,
    extractor: Callable[[dict[str, Any]], Optional[float]],
    *,
    worsening_sign: int,
    threshold: Optional[float] = None,
    anchor_resets: bool = False,
    now: Optional[datetime] = None,
    counter: bool = False,
    min_span_h: float = 24.0,
    floor_abs: Optional[float] = None,
    floor_rel: float = 0.10,
    min_distinct: int = 1,
    runaway_floor: Optional[float] = None,
    level_readings: int = 1,
) -> TrendResult:
    """Compute one metric's significant trend + (optional) ETA to a threshold.

    ``worsening_sign`` = +1 when *rising* is bad (wear, boot time), -1 when
    *falling* is bad (free space, NVMe spare). ``threshold`` is the failure
    boundary of a depletion metric; omit it where only the direction means
    something. ``anchor_resets`` restarts the series after a reset step: a part
    swap for wear, a cleanup for free space. Defaults are the generic gauge;
    ``counter`` series take the sign of a significant z (``_counter_rule``).
    """
    points = _to_points(series, extractor)
    if anchor_resets:
        points = _anchor_recent_segment(points, worsening_sign, _RESET_STEP)
    breach = _level_breach(metric, points, worsening_sign, threshold, now)
    if breach is not None:
        return breach
    slope = theil_sen_slope(points)
    gate = _gate_reason(points, slope, None if counter else min_span_h)
    if gate or slope is None:
        return _insufficient(metric, points, gate)
    recent = theil_sen_slope(points[len(points) // 2 :])
    if counter:
        verdict = _counter_rule(points, slope, worsening_sign, threshold)
    else:
        eff = _effective_slope(points, slope, recent, worsening_sign, runaway_floor)
        material = eff is not None and _material(eff, points[-1][1], floor_abs, floor_rel)
        gated = eff if material else None
        verdict = _gauge_rule(
            points, slope, gated, worsening_sign, threshold, min_distinct, level_readings
        )
    return _result(metric, points, verdict, slope, recent, worsening_sign, now)


def _eta_to_risk(eta_days: float) -> float:
    for horizon, risk in _ETA_RISK_BANDS:
        if eta_days <= horizon:
            return risk
    return _ETA_RISK_FAR


# --------------------------------------------------------------------------- #
# Aggregate: the trajectory_risk Score100 axis (W4 -> W0.5 envelope)
# --------------------------------------------------------------------------- #
def compute_trends(
    historical_series: list[dict[str, Any]],
    heartbeat_series: list[dict[str, Any]],
    *,
    disk_series: Optional[list[dict[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> dict[str, TrendResult]:
    """All deterministic depletion/degradation trends for one device.

    ``disk_series`` (Ф2, ssd3): one physical disk's own append-only readings
    (newest-first, from ``db.get_disk_series``) -- the worst-disk selected by
    ``storage.worst_disk_key``. Absent for pre-Ф2 callers, so the four
    disk-level keys below are simply missing from the result (never
    fabricated); ``storage.compute_storage_risk`` handles that via ``.get()``.
    """
    trends: dict[str, TrendResult] = {
        # Integer wear % steps slowly: 72 h of history and >= 4 distinct values,
        # else 'flat' (months of constant wear are known-stable).
        "storage_wear": build_trend(
            historical_series,
            "storage_wear",
            _max_storage_wear,
            worsening_sign=1,
            threshold=100.0,
            anchor_resets=True,  # drive replacement resets wear%
            now=now,
            min_span_h=72.0,
            floor_abs=1.0,
            min_distinct=4,
        ),
        "disk_fill": build_trend(
            heartbeat_series,
            "disk_fill",
            _free_space_pct,
            worsening_sign=-1,
            threshold=_DISK_FILL_FLOOR_PCT,
            level_readings=4,  # H7: the fill level is the median of the last 4 readings
            # A +10 pp rise of free space (_RESET_STEP) is a cleanup that restarts
            # the depletion trend; Windows Update staging rebounds are 2-8 pp.
            anchor_resets=True,
            now=now,
            floor_abs=1.0,
            runaway_floor=1.0,  # a runaway fill shows within 72 h at >= 1 pp/day
        ),
        # Per-boot averages step; a day of them is no trend -> 72 h.
        "boot_time": build_trend(
            historical_series, "boot_time", _boot_ms, worsening_sign=1, now=now, min_span_h=72.0
        ),
        "throttle": build_trend(
            heartbeat_series, "throttle", _cpu_perf_pct, worsening_sign=-1, now=now
        ),
        # Phase 2: direction-only (no failure boundary -> no ETA, D10). Rising
        # latency to the gateway = link/switch degradation worth a look.
        "gateway_latency": build_trend(
            historical_series, "gateway_latency", _gateway_latency_ms, worsening_sign=1, now=now
        ),
        # Ф4 (K4/K7): direction-only -- no physical failure boundary for a
        # latency-shape ratio, so it never joins the depletion domains / never
        # drives trajectory_risk (it is Resilience evidence for Ф6, not here).
        "disk_tail_ratio": build_trend(
            heartbeat_series, "disk_tail_ratio", _disk_tail_ratio, worsening_sign=1, now=now
        ),
    }
    if disk_series:
        # Direction-only (K4: dynamics, not levels) -- no failure boundary for
        # attribute counters, so no ETA/depletion-domain membership. Counters:
        # a one-unit step is a genuine change although its Theil-Sen is 0.
        for name, extract in (
            ("smart_pending", _disk_smart_attr("197")),
            ("smart_media_errors", _disk_nvme_media_errors),
            ("smart_realloc", _disk_smart_attr("5")),
        ):
            trends[name] = build_trend(
                disk_series, name, extract, worsening_sign=1, now=now, counter=True
            )
        # K7: spare depletion has a real floor (its own threshold) -> genuine ETA
        # to compensation collapse, so it belongs in the depletion domains too.
        latest_threshold = next(
            (
                float(r["nvme_spare_threshold_pct"])
                for r in disk_series
                if r.get("nvme_spare_threshold_pct") is not None
            ),
            None,
        )
        # A counter, not a gauge: integer spare % moves in steps, so Theil-Sen
        # medians are 0 and a gauge rule never sees a collapse
        # (design/fn_critic/spare_steps.py); spare never replenishes.
        trends["nvme_spare"] = build_trend(
            disk_series,
            "nvme_spare",
            _disk_nvme_spare_pct,
            worsening_sign=-1,
            threshold=latest_threshold,
            now=now,
            counter=True,
        )
    return trends


# Depletion domains drive trajectory_risk (they have a real failure boundary +
# ETA); boot/throttle are surfaced as direction only and do not invent risk.
# Ф2: nvme_spare joins them -- its threshold is a genuine compensation-collapse
# boundary (K7), unlike the direction-only attribute counters above.
_DEPLETION_DOMAINS = ("storage_wear", "disk_fill", "nvme_spare")


def _domain_risk(name: str, eta_days: float) -> float:
    risk = _eta_to_risk(eta_days)
    if name == "storage_wear":
        return min(risk, _WEAR_RISK_CAP)
    return min(risk, _FILL_RISK_CAP) if name == "disk_fill" else risk


def _eta_label(name: str, t: TrendResult) -> str:
    label = f"{name} → {t.eta_days:.0f}д до предела"
    if t.slope_per_day is None:  # a level breach: no slope is behind its direction
        return label
    return f"{label} (наклон {t.slope_per_day:+.3f}/день)"


def _depletion_evidence(
    trends: dict[str, TrendResult],
) -> tuple[list[Factor], list[str], list[float], Optional[float]]:
    """Per-domain factors, missing evidence, ETA risks and the longest usable span
    in days (None = no depletion domain has a usable trend)."""
    factors: list[Factor] = []
    missing: list[str] = []
    risks: list[float] = []
    best_span: Optional[float] = None
    for name in _DEPLETION_DOMAINS:
        t = trends.get(name)
        worn_out = name == "storage_wear" and t is not None and (t.current or 0.0) >= 100.0
        if (
            t is None
            or t.direction == "insufficient"
            or (t.direction == "worsening" and t.eta_days is None and not worn_out)
        ):
            # KodSR M9: ухудшение без ETA (нет порога или < 72 ч) -- это
            # недостаток данных, а не "стабильно" (тот же формат, что insufficient).
            missing.append(f"{name}: недостаточно истории")
            continue
        best_span = max(best_span or 0.0, t.span_days or 0.0)
        if worn_out:
            factors.append({"label": _WEAR_EXHAUSTED, "delta": 0.0})
        if t.direction == "worsening" and t.eta_days is not None:
            risks.append(_domain_risk(name, t.eta_days))
            factors.append({"label": _eta_label(name, t), "delta": risks[-1]})
    return factors, missing, risks, best_span


def trajectory_risk_score(trends: dict[str, TrendResult], *, device_trust: str = "ok") -> Score100:
    """Aggregate the depletion trends into a confidence-gated trajectory_risk axis.

    Higher = a failure boundary is projected *sooner*. Gating mirrors W0.5:
    untrusted identity withholds; no usable depletion trend -> UNKNOWN (never a
    confident 0). The riskiest domain drives the value (storage_wear capped at the
    180-day band); "high" confidence needs >= ``_HIGH_CONF_SPAN_DAYS`` of a trend.
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
            reason="идентификатор устройства не подтверждён (контракт §7)",
        )
    factors, missing, risks, best_span = _depletion_evidence(trends)
    if best_span is None:
        return make_score100(
            None,
            direction,
            "unknown",
            "unknown",
            missing_evidence=missing,
            reason="недостаточно истории ни по одному контролируемому ресурсу",
        )
    # We may have trends but none depleting toward a boundary -> stable (0.0).
    value = max(risks) if risks else 0.0
    confidence: ScoreConfidence = "high" if best_span >= _HIGH_CONF_SPAN_DAYS else "medium"
    return make_score100(
        value,
        direction,
        band_for_risk_score(value),
        confidence,
        factors=factors,
        missing_evidence=missing,
        source_lineage={
            n: {"direction": trends[n].direction, "n_points": trends[n].n_points}
            for n in _DEPLETION_DOMAINS
            if n in trends
        },
        reason="" if risks else "истощение ресурса не прогнозируется; тренды стабильны",
    )
