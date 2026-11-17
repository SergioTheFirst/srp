"""Health coordinates (spec 2026-10-30, moved out of health.py unchanged): the
Observability, Damage and Resilience assembly over the serialised risk signals, and
their small helpers. Pure; health.py imports it (and re-exports for its tests)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from server.analytics.health_dominant import _ax, _ax_val
from server.analytics.health_ladder import _HARD_D_FLAGS, _TRAJ_MULT, _storage_resilience
from server.scoring.score100 import band_for_risk_score

_MATURE_POINTS = 6
_COHORT_MIN = 5
_AGING_RLOSS_CAP = 39.0  # software aging is not device damage: alone it keeps R > 60 (D2)
_OS_MULT = 0.6
_CONF_RANK = {"unknown": 0, "low": 1, "medium": 2, "high": 3}
_RANK_CONF = {v: k for k, v in _CONF_RANK.items()}
_MSG_IMMATURE = "оценка устойчивости незрелая"
_NO_SMART = "SMART недоступен"
# Resilience-maturity trends (Ф2/Ф4 dynamics); no mature one -> estimate immature.
_KEY_TRENDS = (
    "smart_pending",
    "smart_media_errors",
    "smart_realloc",
    "nvme_spare",
    "disk_tail_ratio",
)
# Trends built from heartbeat rows (trends.compute_trends): their points are heartbeats
# (throttle: only those with a loaded CPU since H3 -- the aging lineage covers idle ones).
_HB_TRENDS = ("disk_fill", "throttle", "disk_tail_ratio")


@dataclass(frozen=True)
class Coordinate:
    value: Optional[float]  # D: higher=worse; R/O: higher=better
    band: str  # good/watch/bad/unknown
    confidence: str  # high/medium/low/unknown
    evidence: list[dict] = field(default_factory=list)  # russian factors
    flags: list[str] = field(default_factory=list)  # english machine-flags
    parts: dict = field(default_factory=dict)  # H8: channel -> its D / R-loss share


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _worst_conf(confs: list) -> str:
    ranks = [_CONF_RANK.get(c, 0) for c in confs]
    return _RANK_CONF[min(ranks)] if ranks else "unknown"


def _cap(conf: str, ceiling: str) -> str:
    return _RANK_CONF[min(_CONF_RANK.get(conf, 0), _CONF_RANK.get(ceiling, 0))]


def _top(facts: list, n: int) -> list:
    uniq: dict[str, dict] = {}
    for f in facts:
        label = f.get("label") if isinstance(f, dict) else None
        if label and label not in uniq:
            uniq[label] = f
    return sorted(uniq.values(), key=lambda x: abs(x.get("delta") or 0.0), reverse=True)[:n]


def _dedup(items: list) -> list:
    out: list = []
    for i in items:
        if i not in out:
            out.append(i)
    return out


# --------------------------------------------------------------------------- #
# Step 1: Observability (first; K5)
# --------------------------------------------------------------------------- #
def _events_live(errchain: Optional[dict]) -> bool:
    """The events O-channel: something counted, or the collector delivered (H4)."""
    if not isinstance(errchain, dict):
        return False
    counts = errchain.get("counts")
    seen = (isinstance(counts, dict) and any(counts.values())) or bool(errchain.get("factors"))
    return seen or errchain.get("delivered") is True


def _observability(
    axes: dict, trends: dict, errchain: Optional[dict], cohort: Optional[dict]
) -> tuple[Coordinate, list]:
    depth_live = any(
        (trends.get(t) or {}).get("n_points", 0) >= _MATURE_POINTS for t in _KEY_TRENDS
    )
    cohort_live = cohort is not None and (cohort.get("cohort_size") or 0) >= _COHORT_MIN
    # P0-9: reflect DATA presence, not container presence. In prod `errchain` is
    # always a non-None asdict(chain) whose `counts` is a fixed 4-key dict (all
    # zero when no events), and `axes` always carries every engine's key -- so
    # `bool(errchain)`/`bool(axes)` are always truthy. Require real content: some
    # counted event (or factor), and at least one non-None axis value.
    errchain_live = _events_live(errchain)
    axes_live = any(isinstance(v, dict) and v.get("value") is not None for v in axes.values())
    # (weight, is_live, russian blind-spot string, the live channel's name)
    channels = [
        (0.35, _ax_val(axes, "storage_risk") is not None, _NO_SMART, "SMART диска"),
        (0.15, errchain_live, "нет анализа событий", "анализ событий"),
        (
            0.15,
            _perf_live(axes, trends),
            "нет телеметрии производительности",
            "телеметрия производительности",
        ),
        (0.15, depth_live, "нет истории трендов", "история трендов"),
        (0.10, axes_live, "нет инвентаризации/идентификации", "инвентаризация/идентификация"),
        (0.10, cohort_live, "нет когорты для сравнения", "когорта для сравнения"),
    ]
    value = round(sum(w for w, live, _, _ in channels if live) * 100, 1)
    blind = [msg for w, live, msg, _ in channels if not live]
    # the channel is named: a reason is "what is seen", and on the public stand, where
    # "(вес N)" is cut, unnamed labels collapsed into one phrase repeated N times
    evidence = [
        {"label": f"{name} (вес {w:.2f})", "delta": w} for w, live, _, name in channels if live
    ]
    band = "good" if value >= 75 else "watch" if value >= 40 else "bad"
    conf = "high" if value >= 75 else "medium" if value >= 40 else "low"
    return Coordinate(value, band, conf, evidence[:5], []), blind


def _perf_live(axes: dict, trends: dict) -> bool:
    """Heartbeats arrive: the aging value, its short-session row count, or heartbeat rows
    in the trends -- the aging engine is UNKNOWN below 4 usable session rows
    (daily-shutdown PCs, D2). Untrusted: no."""
    aging = _ax(axes, "software_aging_risk")
    if aging.get("value") is not None:
        return True
    lineage = aging.get("source_lineage") or {}
    if lineage.get("identity") == "untrusted":
        return False
    if lineage.get("perf_rows"):
        return True
    return any((trends.get(key) or {}).get("n_points") for key in _HB_TRENDS)


# --------------------------------------------------------------------------- #
# Step 2: Damage
# --------------------------------------------------------------------------- #
def _damage(axes: dict) -> Coordinate:
    channels: list[tuple[float, str, list, list]] = []
    st = _ax(axes, "storage_risk")
    if st.get("value") is not None:
        coords = st.get("coords") or {}
        d_flags = [f for f in (coords.get("flags") or []) if f in _HARD_D_FLAGS]
        channels.append(
            (
                float(coords.get("damage") or 0.0),
                st.get("confidence", "unknown"),
                st.get("factors") or [],
                d_flags,
            )
        )
    os_ = _ax(axes, "os_degradation_risk")
    names = ["storage"] if channels else []
    if os_.get("value") is not None:
        names.append("os")
        channels.append(
            (
                float(os_["value"]) * _OS_MULT,
                os_.get("confidence", "unknown"),
                os_.get("factors") or [],
                [],
            )
        )
    if not channels:
        return Coordinate(None, "unknown", "unknown", [], [])
    d = max(c[0] for c in channels)
    conf = _worst_conf([c[1] for c in channels])
    flags = _dedup([f for c in channels for f in c[3]])
    evidence = _top([f for c in channels for f in c[2]], 5)
    parts = {n: round(c[0], 1) for n, c in zip(names, channels)}  # H8
    return Coordinate(round(d, 1), band_for_risk_score(d), conf, evidence, flags, parts)


# --------------------------------------------------------------------------- #
# Step 3: Resilience
# --------------------------------------------------------------------------- #
def _key_trends_immature(trends: dict) -> bool:
    # n_points 0 / None = a counter family this disk does not report: absent, not thin
    points = [n for n in ((trends.get(t) or {}).get("n_points") for t in _KEY_TRENDS) if n]
    if not points:
        return True  # no key series at all -> estimate is immature
    return any(n < _MATURE_POINTS for n in points)


def _resilience(axes: dict, trends: dict) -> tuple[Coordinate, list, bool]:
    channels: list[tuple[float, str, list, list]] = []
    names: list[str] = []
    st = _ax(axes, "storage_risk")
    if st.get("value") is not None:
        names.append("storage")
        channels.append(_storage_resilience(st, trends))
    ag = _ax(axes, "software_aging_risk")
    if ag.get("value") is not None:
        names.append("aging")
        coords = ag.get("coords") or {}
        channels.append(
            (
                min(float(ag["value"]), _AGING_RLOSS_CAP),  # aging alone never reaches h2
                ag.get("confidence", "unknown"),
                ag.get("factors") or [],
                list(coords.get("flags") or []),
            )
        )
    tr = _ax(axes, "trajectory_risk")
    if tr.get("value") is not None:
        names.append("trajectory")
        channels.append(
            (
                float(tr["value"]) * _TRAJ_MULT,
                tr.get("confidence", "unknown"),
                tr.get("factors") or [],
                [],
            )
        )
    immature = _key_trends_immature(trends)
    if not channels:
        return Coordinate(None, "unknown", "unknown", [], []), [], immature
    rloss = max(c[0] for c in channels)
    conf = _worst_conf([c[1] for c in channels])
    if immature:
        conf = _cap(conf, "medium")
    flags = _dedup([f for c in channels for f in c[3]])
    evidence = _top([f for c in channels for f in c[2]], 5)
    parts = {n: round(c[0], 1) for n, c in zip(names, channels)}  # H8: R-loss per channel
    value = round(100.0 - rloss, 1)
    coord = Coordinate(value, band_for_risk_score(rloss), conf, evidence, flags, parts)
    return coord, ([_MSG_IMMATURE] if immature else []), immature
