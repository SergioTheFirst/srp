"""W4.1 deterministic trend engine: slopes + ETA over append-only history.

The first genuine early-warning value (cctodo §4.1): now that P0 gives a trusted,
server-time, append-only series per device, we can ask *"is this getting worse,
and how fast?"* purely arithmetically -- no ML, no training (this is D4).

Design rules (telemetry-trust-contract + CLAUDE §5):
  * **Robust** slope (Theil-Sen median of pairwise slopes) -- one bad reading or a
    counter reset must not invent a trend.
  * **Server time** x-axis (``received_at``, W0.2) -- never trust the client clock.
  * **UNKNOWN over false confidence** -- below ``_MIN_POINTS`` samples, or a flat /
    improving slope, we assert *no* ETA rather than a fabricated one.
  * The aggregate ``trajectory_risk`` reuses the W0.5 Score100 envelope (same
    gating: untrusted identity withholds; insufficient data -> UNKNOWN).

No nested confidence calculus -- simple, explicit thresholds only (contract §13).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any, Callable, Optional

from server.scoring.score100 import (
    Direction,
    Factor,
    Score100,
    ScoreConfidence,
    band_for_risk_score,
    make_score100,
)

# Below this many usable samples a slope is noise, not a trend -> UNKNOWN.
_MIN_POINTS = 3
# A worsening domain needs >= this many samples before we call the trend "high"
# confidence; fewer is still reported but flagged medium.
_HIGH_CONF_POINTS = 6
_SECONDS_PER_DAY = 86400.0

# ETA (days to threshold) -> trajectory risk 0..100 (higher = sooner = worse).
# Deterministic horizon bands an operator can act on, not a probability.
_ETA_RISK_BANDS = ((30.0, 90.0), (90.0, 60.0), (180.0, 35.0), (365.0, 15.0))
_ETA_RISK_FAR = 5.0  # depleting, but > 1 year out


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
    point is the current reading.
    """
    points: list[tuple[float, float]] = []
    for row in series:
        when = _parse_iso(row.get("received_at") or row.get("ts"))
        if when is None:
            continue
        value = extractor(row)
        if value is None:
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


# A step opposite the worsening direction larger than this (in metric units) is a
# hardware reset, not a trend: a drive swap drops wear% ~85->~5, a battery swap
# resets FCC. Wear/power-on counters are physically monotonic, so any real drop is
# a new part -- we anchor on the most recent segment rather than averaging across it.
_RESET_STEP = 10.0


def _anchor_recent_segment(
    points: list[tuple[float, float]], worsening_sign: int, reset_step: float
) -> list[tuple[float, float]]:
    """Keep only the points after the last reset (monotonic-counter replacement).

    Theil-Sen survives a single outlier but not a sustained level shift; for a
    counter that only moves one way in normal life (wear, power-on hours) a move
    the *other* way past ``reset_step`` means the part was replaced.
    """
    for i in range(len(points) - 1, 0, -1):
        step = points[i][1] - points[i - 1][1]
        if step * worsening_sign < -reset_step:
            return points[i:]
    return points


# --------------------------------------------------------------------------- #
# Per-metric trend
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TrendResult:
    metric: str
    n_points: int
    current: Optional[float]
    slope_per_day: Optional[float]
    eta_days: Optional[float]
    target_date: Optional[str]  # ISO date of the projected crossing, if any
    direction: str  # "worsening" | "improving" | "flat" | "insufficient"
    reason: str = ""


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
    }


def build_trend(
    series: list[dict[str, Any]],
    metric: str,
    extractor: Callable[[dict[str, Any]], Optional[float]],
    *,
    worsening_sign: int,
    threshold: Optional[float] = None,
    anchor_resets: bool = False,
    now: Optional[datetime] = None,
) -> TrendResult:
    """Compute one metric's robust trend + (optional) ETA to a threshold.

    ``worsening_sign`` = +1 when *rising* is bad (wear, boot time), -1 when
    *falling* is bad (free space, CPU performance). ``threshold`` is the failure
    boundary for depletion metrics (100% wear, 0% free); omit it for unbounded
    metrics (boot ms) where only the direction is meaningful. ``anchor_resets``
    trims history before a part replacement (only for physically-monotonic
    counters like wear -- not free space, which legitimately rises on cleanup).
    """
    points = _to_points(series, extractor)
    if anchor_resets:
        points = _anchor_recent_segment(points, worsening_sign, _RESET_STEP)
    n = len(points)
    if n < _MIN_POINTS:
        return TrendResult(
            metric,
            n,
            points[-1][1] if points else None,
            None,
            None,
            None,
            "insufficient",
            f"need >= {_MIN_POINTS} readings, have {n}",
        )

    slope = theil_sen_slope(points)
    current = points[-1][1]
    if slope is None:
        return TrendResult(
            metric,
            n,
            current,
            None,
            None,
            None,
            "insufficient",
            "readings not separated in time",
        )

    toward_failure = slope * worsening_sign > 0
    if not toward_failure:
        direction = "improving" if slope * worsening_sign < 0 else "flat"
        return TrendResult(metric, n, current, slope, None, None, direction)

    eta: Optional[float] = None
    if threshold is not None:
        # Already at/past the boundary while still worsening -> imminent, not
        # "no crossing": a drive at 100% wear must not read as zero trajectory risk.
        crossed = (worsening_sign > 0 and current >= threshold) or (
            worsening_sign < 0 and current <= threshold
        )
        eta = 0.0 if crossed else eta_days_to_threshold(current, slope, threshold)
    target_date: Optional[str] = None
    if eta is not None:
        base = now or datetime.now(timezone.utc)
        target_date = (base + timedelta(days=eta)).date().isoformat()
    return TrendResult(metric, n, current, slope, eta, target_date, "worsening")


# --------------------------------------------------------------------------- #
# Metric extractors (over the spread historical / heartbeat series rows)
# --------------------------------------------------------------------------- #
def _max_storage_wear(row: dict[str, Any]) -> Optional[float]:
    worst: Optional[float] = None
    for disk in row.get("storage") or []:
        w = disk.get("wear_pct") if isinstance(disk, dict) else None
        if w is not None:
            worst = float(w) if worst is None else max(worst, float(w))
    return worst


def _battery_wear(row: dict[str, Any]) -> Optional[float]:
    bat = row.get("battery")
    if not isinstance(bat, dict) or not bat.get("present"):
        return None
    w = bat.get("wear_pct")
    return float(w) if w is not None else None


def _boot_ms(row: dict[str, Any]) -> Optional[float]:
    v = row.get("avg_boot_ms")
    return float(v) if v is not None else None


def _free_space_pct(row: dict[str, Any]) -> Optional[float]:
    v = row.get("free_space_pct")
    return float(v) if v is not None else None


def _cpu_perf_pct(row: dict[str, Any]) -> Optional[float]:
    v = row.get("cpu_perf_pct")
    return float(v) if v is not None else None


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
    now: Optional[datetime] = None,
) -> dict[str, TrendResult]:
    """All deterministic depletion/degradation trends for one device."""
    return {
        "storage_wear": build_trend(
            historical_series,
            "storage_wear",
            _max_storage_wear,
            worsening_sign=1,
            threshold=100.0,
            anchor_resets=True,  # drive replacement resets wear%
            now=now,
        ),
        "battery_wear": build_trend(
            historical_series,
            "battery_wear",
            _battery_wear,
            worsening_sign=1,
            threshold=100.0,
            anchor_resets=True,  # battery replacement resets FCC/wear%
            now=now,
        ),
        "disk_fill": build_trend(
            heartbeat_series,
            "disk_fill",
            _free_space_pct,
            worsening_sign=-1,
            threshold=0.0,
            now=now,
        ),
        "boot_time": build_trend(
            historical_series,
            "boot_time",
            _boot_ms,
            worsening_sign=1,
            now=now,
        ),
        "throttle": build_trend(
            heartbeat_series,
            "throttle",
            _cpu_perf_pct,
            worsening_sign=-1,
            now=now,
        ),
    }


# Depletion domains drive trajectory_risk (they have a real failure boundary +
# ETA); boot/throttle are surfaced as direction only and do not invent risk.
_DEPLETION_DOMAINS = ("storage_wear", "battery_wear", "disk_fill")


def trajectory_risk_score(
    trends: dict[str, TrendResult],
    *,
    device_trust: str = "ok",
) -> Score100:
    """Aggregate the depletion trends into a confidence-gated trajectory_risk axis.

    Higher = a failure boundary is projected *sooner*. Gating mirrors W0.5:
    untrusted identity withholds entirely; no usable trend in any depletion
    domain -> UNKNOWN (never a confident 0). The soonest ETA drives the value;
    every contributing domain is listed as an explainable factor.
    """
    direction: Direction = "higher_is_worse"

    if device_trust == "untrusted":
        return make_score100(
            None,
            direction,
            "unknown",
            "unknown",
            missing_evidence=["identity trust failed"],
            source_lineage={"identity": "untrusted"},
            reason="device identity untrusted (contract §7)",
        )

    factors: list[Factor] = []
    missing: list[str] = []
    soonest_eta: Optional[float] = None
    best_points = 0
    have_any_trend = False

    for name in _DEPLETION_DOMAINS:
        t = trends.get(name)
        if t is None or t.direction == "insufficient":
            missing.append(f"{name} insufficient history")
            continue
        have_any_trend = True
        best_points = max(best_points, t.n_points)
        if t.direction == "worsening" and t.eta_days is not None:
            risk = _eta_to_risk(t.eta_days)
            factors.append(
                {
                    "label": f"{name} -> {t.eta_days:.0f}d to limit "
                    f"(slope {t.slope_per_day:+.3f}/day)",
                    "delta": risk,
                }
            )
            soonest_eta = t.eta_days if soonest_eta is None else min(soonest_eta, t.eta_days)

    if not have_any_trend:
        return make_score100(
            None,
            direction,
            "unknown",
            "unknown",
            missing_evidence=missing,
            reason="insufficient longitudinal history for any depletion domain",
        )

    # We may have trends but none depleting toward a boundary -> stable (0.0).
    value = 0.0 if soonest_eta is None else _eta_to_risk(soonest_eta)

    confidence: ScoreConfidence = "high" if best_points >= _HIGH_CONF_POINTS else "medium"
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
        reason="" if soonest_eta is not None else "no depletion projected; trends stable",
    )
