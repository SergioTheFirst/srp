"""ssd3 Ф6 -- assemble coordinate-tagged signals into the three health
coordinates (Damage, Resilience, Observability) and their derivatives
(state / index / band / horizon / action).

This is a PURE computation module. Every input is an already-serialised ``dict``
(the shapes that land in the persisted ``risk_block``): there is NO I/O, no
``server.db`` import, no network. ``compute_health`` is the single place the
(D, R, O) assembly and all K1 derivations happen; wiring it into the live
pipeline is a separate, later task.

Fixed order (K1, non-negotiable):
    O -> D -> R -> state=f(D,R,O) -> ratchet -> index=f(D,R) -> reconciliation
    -> dominant -> horizon -> action.

Two interface points the plan leaves for the wiring task (documented for review):
* Heartbeat-percentile liveness (observability) is proxied by the presence of
  the ``software_aging_risk`` axis value -- Ф4 derives that axis from exactly
  those percentiles, so its presence is a faithful liveness signal.
* The ratchet's "worst-disk replaced" evidence compares the current worst disk
  (``score100_axes["storage_risk"]["source_lineage"]["worst_disk"]``) against
  ``prev_health["worst_disk"]``. ``HealthVerdict`` as specified carries no disk
  field, so the wiring layer (``server/pipeline.py``) persists ``worst_disk`` as
  an extra key alongside ``asdict(verdict)`` in the stored health blob -- this
  branch, the ``reboot_restores`` branch, and the flat-counter branch are all
  active.
* The tail-ratio surcharge fires on a worsening ``disk_tail_ratio`` trend. Its
  "mean is calm" sub-condition needs a mean ``disk_read_sec`` that is not among
  this function's inputs (no heartbeat is passed here), so the surcharge is
  applied unconditionally as a conservative fail-safe -- it errs toward
  flagging more, never toward hiding a real signal (see the comment at the
  surcharge site in ``_storage_resilience``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from server.scoring.score100 import band_for_risk_score

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
_MATURE_POINTS = 6
_COHORT_MIN = 5
_SPARE_SURCHARGE = 25.0
_TAIL_SURCHARGE = 20.0
_FLAT_SLOPE = 0.01
_TRAJ_MULT = 0.85
_OS_MULT = 0.6

_STATE_ORDER = {"h0": 0, "h1": 1, "h2": 2, "h3": 3, "h4": 4}
_RANK_STATE = {v: k for k, v in _STATE_ORDER.items()}
_CONF_RANK = {"unknown": 0, "low": 1, "medium": 2, "high": 3}
_RANK_CONF = {v: k for k, v in _CONF_RANK.items()}

# Hard Damage flags (storage coords carry D and R flags in one flat list; these
# are the D ones -- everything else on the list is a Resilience flag).
_HARD_D_FLAGS = frozenset(
    {
        "predict_fail",
        "cw_reliability",
        "cw_readonly",
        "pending_gt10",
        "uncorrectable_198",
        "damage_present",
    }
)
# State-criterion flag sets (first match wins in _state, top to bottom).
_H4_FLAGS = frozenset(
    {
        "predict_fail",
        "cw_spare",
        "cw_reliability",
        "cw_readonly",
        "pending_gt10",
        "uncorrectable_198",
        "spare_below_threshold",
        "chain_stage3",
    }
)
_H3_FLAGS = frozenset({"accel", "chain_stage2", "recurrence", "aging_accelerating"})
_H2_FLAGS = frozenset(
    {"remap_masking", "spare_depleting", "tail_ratio_worsening", "early_events", "aging_leak"}
)

# Resilience-maturity trends (Ф2/Ф4 dynamics); no mature one -> estimate immature.
_KEY_TRENDS = (
    "smart_pending",
    "smart_media_errors",
    "smart_realloc",
    "nvme_spare",
    "disk_tail_ratio",
)
# Depletion-domain trends feeding the horizon ETA rule.
_DEPLETION_TRENDS = ("nvme_spare", "disk_fill", "battery_wear", "storage_wear")
# Damage counters whose flatness is ratchet-improvement evidence (197 / 5 / media).
_FLAT_COUNTER_TRENDS = ("smart_pending", "smart_realloc", "smart_media_errors")

# score100 axis name -> short dominant machine-key.
_AXIS_TO_DOMINANT = {
    "storage_risk": "storage",
    "software_aging_risk": "aging",
    "os_degradation_risk": "os",
    "disk_fill_risk": "disk_fill",
    "battery_risk": "battery",
    "network_risk": "network",
    "trajectory_risk": "trajectory",
}
_SEVERITY = {"network_risk": 0.6, "trajectory_risk": 0.85}

_ACTIONS: dict[Optional[str], str] = {
    "storage": "снять образ данных, планировать замену накопителя",
    "aging": "перезагрузить; если повторится — искать утечку в ПО",
    "os": "переустановка/восстановление ОС при повторении",
    "disk_fill": "освободить место — под угрозой обновления Windows",
    "battery": "заменить батарею",
    "network": "проверить линк/кабель/точку доступа",
    "trajectory": "ресурс близок к исчерпанию — планировать обслуживание",
    "systemic": "искать общую причину: питание, перегрев, недавнее обновление, антивирусные сканы",
    None: "данных недостаточно — проверить агент/доступность источников",
}
_DOMINANT_LABELS: dict[Optional[str], str] = {
    "storage": "накопитель",
    "battery": "батарея",
    "disk_fill": "заполнение диска",
    "os": "операционная система",
    "network": "сеть",
    "trajectory": "траектория ресурса",
    "aging": "старение ПО",
    "systemic": "системная причина",
    None: "не определено",
}
_STATE_LABELS = {
    "unknown": "нет видимости",
    "h4": "предотказ",
    "h3": "ускоренная деградация",
    "h2": "компенсация",
    "h1": "ранняя деградация",
    "h0": "здоров",
}
_HORIZON_STATE_DAYS = {"h4": 7, "h3": 30, "h2": 90}
_HORIZON_STATE_REASON = {
    "h4": "предотказ — прогноз горизонта 7 дней",
    "h3": "ускоренная деградация — прогноз горизонта 30 дней",
    "h2": "истощение компенсации — прогноз горизонта 90 дней",
}
_HORIZON_ETA_REASON = {
    30: "истощение ресурса в пределах 30 дней",
    90: "истощение ресурса в пределах 90 дней",
}
_BLIND_ACTION = "восстановить видимость: проверить агент и доступ к SMART/журналам"
_MSG_IMMATURE = "оценка устойчивости незрелая"
_MSG_PARTIAL = "неполные координаты: точность снижена"


# --------------------------------------------------------------------------- #
# Dataclasses (verbatim from the plan)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Coordinate:
    value: Optional[float]  # D: higher=worse; R/O: higher=better
    band: str  # good/watch/bad/unknown
    confidence: str  # high/medium/low/unknown
    evidence: list[dict] = field(default_factory=list)  # russian factors
    flags: list[str] = field(default_factory=list)  # english machine-flags


@dataclass(frozen=True)
class HealthVerdict:
    damage: Coordinate
    resilience: Coordinate
    observability: Coordinate
    blind_spots: list[str] = field(default_factory=list)
    state: str = "unknown"
    state_label: str = ""
    state_evidence: list[dict] = field(default_factory=list)
    index: Optional[float] = None
    band: str = "unknown"
    confidence: str = "unknown"
    dominant: Optional[str] = None
    dominant_label: str = ""
    horizon_days: Optional[int] = None
    horizon_reason: str = ""
    action: str = ""
    factors: list[dict] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _ax(axes: dict, name: str) -> dict:
    a = axes.get(name)
    return a if isinstance(a, dict) else {}


def _ax_val(axes: dict, name: str) -> Optional[float]:
    v = _ax(axes, name).get("value")
    return float(v) if isinstance(v, (int, float)) else None


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
def _observability(
    axes: dict, trends: dict, errchain: Optional[dict], cohort: Optional[dict]
) -> tuple[Coordinate, list]:
    depth_live = any(
        (trends.get(t) or {}).get("n_points", 0) >= _MATURE_POINTS for t in _KEY_TRENDS
    )
    cohort_live = cohort is not None and (cohort.get("cohort_size") or 0) >= _COHORT_MIN
    # (weight, is_live, russian blind-spot string)
    channels = [
        (0.35, _ax_val(axes, "storage_risk") is not None, "SMART недоступен"),
        (0.15, bool(errchain), "нет анализа событий"),
        (
            0.15,
            _ax_val(axes, "software_aging_risk") is not None,
            "нет телеметрии производительности",
        ),
        (0.15, depth_live, "нет истории трендов"),
        (0.10, bool(axes), "нет инвентаризации/идентификации"),
        (0.10, cohort_live, "нет когорты для сравнения"),
    ]
    value = round(sum(w for w, live, _ in channels if live) * 100, 1)
    blind = [msg for w, live, msg in channels if not live]
    evidence = [
        {"label": f"источник данных активен (вес {w:.2f})", "delta": w}
        for w, live, _ in channels
        if live
    ]
    band = "good" if value >= 75 else "watch" if value >= 40 else "bad"
    conf = "high" if value >= 75 else "medium" if value >= 40 else "low"
    return Coordinate(value, band, conf, evidence[:5], []), blind


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
    bat = _ax(axes, "battery_risk")
    if bat.get("value") is not None:
        channels.append(
            (float(bat["value"]), bat.get("confidence", "unknown"), bat.get("factors") or [], [])
        )
    os_ = _ax(axes, "os_degradation_risk")
    if os_.get("value") is not None:
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
    return Coordinate(round(d, 1), band_for_risk_score(d), conf, evidence, flags)


# --------------------------------------------------------------------------- #
# Step 3: Resilience
# --------------------------------------------------------------------------- #
def _key_trends_immature(trends: dict) -> bool:
    points = [(trends.get(t) or {}).get("n_points", 0) for t in _KEY_TRENDS if trends.get(t)]
    if not points:
        return True  # no key series at all -> estimate is immature
    return any(n < _MATURE_POINTS for n in points)


def _storage_resilience(st: dict, trends: dict) -> tuple[float, str, list, list]:
    coords = st.get("coords") or {}
    loss = float(coords.get("resilience_loss") or 0.0)
    flags = [f for f in (coords.get("flags") or []) if f not in _HARD_D_FLAGS]
    if (trends.get("nvme_spare") or {}).get("direction") == "worsening":
        loss += _SPARE_SURCHARGE
        flags.append("spare_depleting")
    if (trends.get("disk_tail_ratio") or {}).get("direction") == "worsening":
        # ssd3.md's "AND mean is calm" half needs a heartbeat disk_read_sec signal
        # that compute_health's 6-parameter interface does not receive. Applied
        # unconditionally as a conservative fail-safe: this errs toward flagging
        # more, never toward hiding a real signal. Revisit if/when the wiring
        # task decides to pass a latency signal through.
        loss += _TAIL_SURCHARGE
        flags.append("tail_ratio_worsening")
    return min(loss, 100.0), st.get("confidence", "unknown"), st.get("factors") or [], flags


def _resilience(axes: dict, trends: dict) -> tuple[Coordinate, list, bool]:
    channels: list[tuple[float, str, list, list]] = []
    st = _ax(axes, "storage_risk")
    if st.get("value") is not None:
        channels.append(_storage_resilience(st, trends))
    ag = _ax(axes, "software_aging_risk")
    if ag.get("value") is not None:
        coords = ag.get("coords") or {}
        channels.append(
            (
                float(ag["value"]),
                ag.get("confidence", "unknown"),
                ag.get("factors") or [],
                list(coords.get("flags") or []),
            )
        )
    tr = _ax(axes, "trajectory_risk")
    if tr.get("value") is not None:
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
    coord = Coordinate(round(100.0 - rloss, 1), band_for_risk_score(rloss), conf, evidence, flags)
    return coord, ([_MSG_IMMATURE] if immature else []), immature


# --------------------------------------------------------------------------- #
# Step 4: State
# --------------------------------------------------------------------------- #
def _state(d: Coordinate, r: Coordinate, o_val: float, flags: set) -> tuple[str, str, list]:
    ev = _top(list(d.evidence) + list(r.evidence), 5)
    if o_val < 40:
        return "unknown", _STATE_LABELS["unknown"], []
    rv = r.value
    if flags & _H4_FLAGS:
        return "h4", _STATE_LABELS["h4"], ev
    if (flags & _H3_FLAGS) or (rv is not None and rv <= 30):
        return "h3", _STATE_LABELS["h3"], ev
    if (flags & _H2_FLAGS) or (rv is not None and rv <= 60):
        return "h2", _STATE_LABELS["h2"], ev
    if (d.value is not None and d.value >= 15) or ("damage_present" in flags):
        return "h1", _STATE_LABELS["h1"], ev
    return "h0", _STATE_LABELS["h0"], []


# --------------------------------------------------------------------------- #
# Step 5: Ratchet (hysteresis)
# --------------------------------------------------------------------------- #
def _counters_flat(trends: dict) -> bool:
    present = [trends.get(t) for t in _FLAT_COUNTER_TRENDS if trends.get(t)]
    if not present:
        return False
    return all(
        (t or {}).get("n_points", 0) >= _MATURE_POINTS
        and (t or {}).get("direction") != "worsening"
        and abs((t or {}).get("slope_per_day") or 0.0) < _FLAT_SLOPE
        for t in present
    )


def _ratchet(
    state: str, prev: Optional[dict], flags: set, trends: dict, cur_disk: Optional[str]
) -> str:
    if state == "unknown" or not prev:
        return state
    prev_state = prev.get("state")
    if prev_state not in _STATE_ORDER:
        return state
    if _STATE_ORDER[state] >= _STATE_ORDER[prev_state]:
        return state  # worsening (or unchanged) is free
    prev_disk = prev.get("worst_disk")
    if cur_disk is not None and prev_disk is not None and cur_disk != prev_disk:
        return state  # new hardware -> full reset permitted
    if ("reboot_restores" in flags) or _counters_flat(trends):
        return _RANK_STATE[_STATE_ORDER[prev_state] - 1]  # exactly one step of improvement
    return prev_state  # no positive evidence -> hold


# --------------------------------------------------------------------------- #
# Step 6: Index
# --------------------------------------------------------------------------- #
def _index(d_val: Optional[float], rloss: Optional[float], o_val: float) -> Optional[float]:
    if o_val < 40:
        return None
    known = [x for x in (d_val, rloss) if x is not None]
    if not known:
        return None
    loss = 0.7 * max(known) + 0.3 * (sum(known) / len(known))
    return max(0.0, min(100.0, round(100.0 - round(loss, 1), 1)))


# --------------------------------------------------------------------------- #
# Step 7: Reconciliation
# --------------------------------------------------------------------------- #
def _reconcile(state: str, index: Optional[float], o_val: float) -> tuple[str, list]:
    if o_val < 40:
        return "unknown", []
    band = band_for_risk_score(100 - index) if index is not None else "unknown"
    factors: list[dict] = []
    if state == "h4":
        band = "bad"
    elif state in ("h2", "h3") and band == "good":
        band = "watch"
    if state == "h0" and band == "bad":  # unreachable by construction -> defensive clamp
        band = "watch"
        factors.append(
            {"label": "защитный клэмп: состояние h0 несовместимо с оценкой bad", "delta": 0}
        )
    return band, factors


# --------------------------------------------------------------------------- #
# Step 9: Dominant / systemic / horizon
# --------------------------------------------------------------------------- #
def _is_systemic(axes: dict, trends: dict) -> bool:
    mechs = sum(
        1
        for name in _AXIS_TO_DOMINANT
        if _ax(axes, name).get("value") is not None
        and _ax(axes, name).get("band") in ("watch", "bad")
    )
    worsening = sum(1 for t in trends.values() if (t or {}).get("direction") == "worsening")
    return mechs >= 3 or worsening >= 3


def _dominant(axes: dict, trends: dict) -> tuple[Optional[str], bool]:
    if _is_systemic(axes, trends):
        return "systemic", True
    best_key: Optional[str] = None
    best_score = 0.0
    for name, short in _AXIS_TO_DOMINANT.items():
        v = _ax_val(axes, name)
        if v is None:
            continue
        score = v * _SEVERITY.get(name, 1.0)
        if score > best_score:
            best_score, best_key = score, short
    return best_key, False


def _horizon(state: str, trends: dict) -> tuple[Optional[int], str]:
    state_days = _HORIZON_STATE_DAYS.get(state)
    eta_days: Optional[int] = None
    for t in _DEPLETION_TRENDS:
        eta = (trends.get(t) or {}).get("eta_days")
        if eta is None:
            continue
        if eta <= 30:
            eta_days = min(eta_days or 999, 30)
        elif eta <= 90:
            eta_days = min(eta_days or 999, 90)
    # state-rules checked before ETA-rules; state wins ties.
    if state_days is not None and (eta_days is None or state_days <= eta_days):
        return state_days, _HORIZON_STATE_REASON[state]
    if eta_days is not None:
        return eta_days, _HORIZON_ETA_REASON[eta_days]
    return None, ""


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
def compute_health(
    score100_axes: dict,
    bayes: dict,
    trends: dict,
    errchain: Optional[dict],
    cohort: Optional[dict],
    prev_health: Optional[dict],
) -> HealthVerdict:
    """Assemble (D, R, O) + derivatives from already-serialised risk signals.

    See the module docstring for the fixed K1 order and the documented
    wiring-side interface points. Pure function: no I/O, no mutation of inputs.
    """
    axes = score100_axes if isinstance(score100_axes, dict) else {}
    trends = trends if isinstance(trends, dict) else {}

    o, blind_spots = _observability(axes, trends, errchain, cohort)  # 1
    d = _damage(axes)  # 2
    r, r_missing, immature = _resilience(axes, trends)  # 3
    all_flags = set(d.flags) | set(r.flags)

    state, _state_label, state_ev = _state(d, r, o.value or 0.0, all_flags)  # 4
    cur_disk = _ax(axes, "storage_risk").get("source_lineage", {}).get("worst_disk")
    state = _ratchet(state, prev_health, all_flags, trends, cur_disk)  # 5

    dominant, systemic = _dominant(axes, trends)  # 9 (needed for systemic floor + action)
    if systemic and state != "unknown" and _STATE_ORDER.get(state, 4) < 2:
        state = "h2"  # systemic floor
    state_label = _STATE_LABELS.get(state, "")  # re-derive after ratchet + systemic floor

    rloss = None if r.value is None else round(100.0 - r.value, 1)
    index = _index(d.value, rloss, o.value or 0.0)  # 6
    band, reconcile_factors = _reconcile(state, index, o.value or 0.0)  # 7
    horizon_days, horizon_reason = _horizon(state, trends)  # 9

    confidence = _confidence(d, r, o.value or 0.0, immature)
    action = _BLIND_ACTION if (o.value or 0.0) < 40 else _ACTIONS.get(dominant, _ACTIONS[None])
    dominant_label = _DOMINANT_LABELS.get(dominant, _DOMINANT_LABELS[None])

    factors = _top(list(d.evidence) + list(r.evidence) + list(o.evidence), 8)
    factors += _context_factors(dominant, bayes, cohort, trends) + reconcile_factors
    missing = _dedup(list(r_missing) + _confidence_missing(d, r, o.value or 0.0))

    return HealthVerdict(
        damage=d,
        resilience=r,
        observability=o,
        blind_spots=blind_spots,
        state=state,
        state_label=state_label,
        state_evidence=state_ev,
        index=index,
        band=band,
        confidence=confidence,
        dominant=dominant,
        dominant_label=dominant_label,
        horizon_days=horizon_days,
        horizon_reason=horizon_reason,
        action=action,
        factors=factors,
        missing_evidence=missing,
    )


def _confidence(d: Coordinate, r: Coordinate, o_val: float, immature: bool) -> str:
    if o_val < 40:
        return "low"
    base = [c.confidence for c in (d, r) if c.value is not None]
    conf = _worst_conf(base) if base else "medium"
    if d.value is None or r.value is None:
        conf = _cap(conf, "medium")
    if 40 <= o_val <= 74:
        conf = _cap(conf, "medium")
    if immature:
        conf = _cap(conf, "medium")
    return conf


def _confidence_missing(d: Coordinate, r: Coordinate, o_val: float) -> list:
    if o_val >= 40 and (d.value is None or r.value is None):
        return [_MSG_PARTIAL]
    return []


def _context_factors(
    dominant: Optional[str], bayes: dict, cohort: Optional[dict], trends: dict
) -> list:
    out: list[dict] = []
    top = (bayes or {}).get("top")
    if top and top != dominant:
        out.append({"label": f"bayes-приоритизатор указывает на {top}", "delta": 0})
    if cohort and (cohort.get("cohort_size") or 0) >= _COHORT_MIN:
        boot = (trends.get("boot_time") or {}).get("current")
        p90 = cohort.get("boot_p90_ms")
        if boot is not None and p90 is not None and boot > p90:
            out.append(
                {"label": "загрузка дольше, чем у 90% похожих устройств (контекст)", "delta": 0}
            )
    return out


# --------------------------------------------------------------------------- #
# Read-side accessors (NOT called by compute_health; used by Ф7 dashboards)
# --------------------------------------------------------------------------- #
def action_for(dominant: Optional[str]) -> str:
    """Public accessor for the Ф6 recommendation table -- read-side consumers (Ф7) map a
    dominant mechanism to its Russian recommendation without duplicating _ACTIONS."""
    return _ACTIONS.get(dominant, _ACTIONS[None])


def health_staleness(score_ts: str, now: datetime) -> Optional[str]:
    """Score-age staleness, applied by callers (API/pages), never inside
    ``compute_health`` (which has no timestamp input).

    Returns:
        * ``None`` when fresh (<= 3 days) or ``score_ts`` is unparseable.
        * ``"данные устарели (N дн.)"`` when 3 < age <= 10 days -- the caller is
          expected to cap confidence at ``low``.
        * ``"проверка недостоверна: данные старше 10 дней"`` when age > 10 days --
          a distinguishable value the caller branches on to treat the whole
          verdict as UNKNOWN.
    """
    ts = _parse_dt(score_ts)
    if ts is None:
        return None
    days = (now - ts).days
    if days > 10:
        return "проверка недостоверна: данные старше 10 дней"
    if days > 3:
        return f"данные устарели ({days} дн.)"
    return None


def _parse_dt(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
