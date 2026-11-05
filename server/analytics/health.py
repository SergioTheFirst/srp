"""ssd3 Ф6 -- assemble coordinate-tagged signals into the three health
coordinates (Damage, Resilience, Observability) and their derivatives
(state / index / band / horizon / action).

This is a PURE computation module. Every input is an already-serialised ``dict``
(the shapes that land in the persisted ``risk_block``): there is NO I/O, no
``server.db`` import, no network and no clock read. ``compute_health`` is the
single place the (D, R, O) assembly and all K1 derivations happen (the coordinate
builders themselves live in ``health_coords``).

Fixed order (K1, non-negotiable):
    O -> D -> R -> state=f(D,R,O) -> hysteresis -> dominant -> index=f(D,R)
    -> reconciliation -> horizon -> action.

Interface points with the wiring layer (``server/pipeline.py``):
* Performance-telemetry liveness (observability) is the ``software_aging_risk``
  value or, for a session too short for that engine, heartbeat rows in the trends.
* Hysteresis lives in ``health_hold``: a held state steps down one level only
  after RELEASE_HOURS of OBSERVED time below it, on the ``now`` pipeline passes
  (no ``now`` -> no release). A cycle blind to the storage (gated axis, axis gone
  on a machine that had a disk, SMART attributes lost, the worst disk dropped while
  pipeline's ``inventory_disk_ids`` still list it) freezes the release; a replaced
  disk resets it. The errchain stays device-wide, except that on a PC with ONE fixed
  disk the replaced disk's own 153/129/7/51 leave it from the swap cycle on (X4,
  pipeline._disk_since; a multi-disk PC cannot tell whose events they are). The disk identity
  rides in the verdict (``worst_disk_id`` / ``disk_ids`` / ``attr_disk_ids`` /
  ``had_disk``), persisted by ``asdict``; pipeline adds ``worst_disk``.
* The tail-ratio surcharge (+20 R-loss) follows a significant worsening
  ``disk_tail_ratio`` trend, but its h2 flag needs corroboration (timeouts, resets,
  masking, chain, growth or a hard media flag): latency alone is at most h1 (Hao,
  FAST'16: 26 % of disks slow down within 87 days).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, NamedTuple, Optional

from server.analytics.health_coords import (
    _COHORT_MIN,
    _NO_SMART,
    Coordinate,
    _cap,
    _damage,
    _dedup,
    _events_live,
    _observability,
    _resilience,
    _top,
    _worst_conf,
)
from server.analytics.health_dominant import (
    _TRAJECTORY_DRIVERS,
    _ax,
    _ax_val,
    _dominant,
    _trajectory_driver,
    chain_stage,
)
from server.analytics.health_hold import (
    PHANTOM_MISSING,
    STATE_ORDER,
    blind_cause,
    damage_seen,
    disk_view,
    forecast_key,
    held_mechanism,
    hold_notes,
    hold_state,
    horizon,
    identity,
    phantom,
    visibility,
)
from server.analytics.health_ladder import (
    _H1_FLAGS,
    _H3_FLAGS,
    _H4_FLAGS,
    _rung,
    rung_source,
)
from server.analytics.stability import BSOD_ACTIONS
from server.scoring.score100 import band_for_risk_score

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #


_ACTIONS: dict[Optional[str], str] = {
    "storage": "снять образ данных, планировать замену накопителя",
    "aging": "перезагрузить; если повторится — искать утечку в ПО",
    "os": "переустановка/восстановление ОС при повторении",
    "disk_fill": "освободить место — под угрозой обновления Windows",
    "network": "проверить линк/кабель/точку доступа",
    "trajectory": "ресурс близок к исчерпанию — планировать обслуживание",
    "systemic": "искать общую причину: питание, перегрев, недавнее обновление, антивирусные сканы",
    None: "данных недостаточно — проверить агент/доступность источников",
}
_DOMINANT_LABELS: dict[Optional[str], str] = {
    "storage": "накопитель",
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
# The blind action and the h0 actions without a horizon (D7): not _ACTIONS keys, and no
# colon -- Zabbix joins "label: action" (server/zabbix/items.py), a blind h0 included.
_BLIND_ACTION = "восстановить видимость — проверить агент и доступ к SMART/журналам"
_OBSERVE_ACTION = "наблюдать — повреждений не выявлено, действий не требуется"
_CHECK_ACTION = "проверить накопитель — повышенные показатели без подтверждённых повреждений"
_FS_ACTION = (  # H6: an isolated Ntfs 55 is the file system, not the medium
    "проверить файловую систему (chkdsk) и корректность выключения ПК; сделать копию важных данных"
)
_MSG_PARTIAL = "неполные координаты: точность снижена"
_NOTE_CHAIN = "цепочка ошибок диска по журналу событий (SMART недоступен)"
_NOTE_FS = "порча файловой системы (Ntfs 55) без подтверждения диском"  # H6, as storage
# H5 / H12: storage without health data (phantom SMART, or no SMART at all)
_NO_HEALTH_DATA = "SMART недоступен (только износ и температура)"
_NOTE_AVAILABLE = "оценка по доступным данным: SMART недоступен"
_MSG_INSUFFICIENT = "недостаточно данных: SMART недоступен"


# --------------------------------------------------------------------------- #
# Dataclasses (verbatim from the plan)
# --------------------------------------------------------------------------- #
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
    # Последнее состояние, отличное от "unknown". Нужно удержанию, чтобы цикл
    # «нет видимости» не стирал память (health_hold.hold_state). Аддитивно.
    last_known_state: Optional[str] = None
    # Additive (C5): horizon kind, observed-time hysteresis clock and the disk
    # identity (carried forward on blind cycles) -- see health_hold.
    horizon_kind: Optional[str] = None  # "state" | "eta" | None
    below_h: Optional[float] = None
    below_seen_at: Optional[str] = None
    worst_disk_id: Optional[str] = None
    disk_ids: list[str] = field(default_factory=list)
    attr_disk_ids: list[str] = field(default_factory=list)
    had_disk: bool = False  # a disk was ever listed (sticky): blindness when the axis is gone
    held_dominant: Optional[str] = None  # mechanism that set the held level (not the label)
    raw_state: Optional[str] = None  # state before the hold: journal, held != raw (H18)
    # H1: why the disks were not seen (loss | offline | unknown), the spent unknown pause
    # of this release window, and a disk replacement this cycle (full reset).
    blind_cause: Optional[str] = None
    unknown_pause_used: bool = False
    disk_replaced: bool = False
    health_seen_ids: list[str] = field(default_factory=list)  # H5: disks that had health data
    cause_class: Optional[str] = None  # H6+: power_likely | unknown (never moves the state)
    # H8: what set the shown level (flag:* / rloss:* / d:* / heavy_storage /
    # systemic_floor / hold / fill_floor (H11) / bsod_days (H13); None for h0 and
    # unknown) and, persisted across a hold,
    # the source of the level being held (it keeps that level's action)
    rung_source: Optional[str] = None
    held_source: Optional[str] = None


# --------------------------------------------------------------------------- #
# Step 4: State
# --------------------------------------------------------------------------- #
def _state(
    d: Coordinate, r: Coordinate, o_val: float, axes: dict, stability: Optional[dict] = None
) -> tuple[str, str, list, Optional[str]]:
    ev = _top(list(d.evidence) + list(r.evidence), 5)
    # Two independent unknown-gates (K5, "UNKNOWN over false confidence"): low
    # observability OR no Damage channel, whatever o_val. Resilience is almost always
    # alive (aging/trajectory): a hidden Damage (o5-E1) must never read as «здоров».
    if o_val < 40 or d.value is None:
        return "unknown", _STATE_LABELS["unknown"], [], None
    # the heavy h2 rung is sized by the storage channel's own damage, never the OS one
    st_damage = float((_ax(axes, "storage_risk").get("coords") or {}).get("damage") or 0.0)
    flags = set(d.flags) | set(r.flags)
    state = _rung(d.value, r.value, flags, st_damage)
    # H8: the rule that set the rung (an isolated Ntfs 55 names it only alone -- H6)
    source = rung_source(state, d.parts, r.parts, flags, st_damage)
    stab = stability or {}
    bsod = stab.get("rung")
    if bsod in STATE_ORDER and STATE_ORDER[bsod] > STATE_ORDER[state]:  # H13: the ladder wins ties
        return bsod, _STATE_LABELS[bsod], [_bsod_note(stab), *ev], "bsod_days"
    fill = _ax(axes, "disk_fill_risk")
    if state == "h0" and (fill.get("source_lineage") or {}).get("below_severe"):
        # H11: a system drive below 8 % is h1 (urgency, no reaction window); the
        # ladder always wins a tie, a gated disk_fill has no level to floor on
        return "h1", _STATE_LABELS["h1"], _top(fill.get("factors") or [], 1), "fill_floor"
    return state, _STATE_LABELS[state], (ev if state != "h0" else []), source


# --------------------------------------------------------------------------- #
# Step 6: Index
# --------------------------------------------------------------------------- #
def _bsod_note(stab: dict) -> dict:
    return {"label": f"BSOD в разные дни: {stab.get('days')} за 30 суток", "delta": 0}


def _gated_bsod(raw: str, stability: Optional[dict]) -> list:
    """H13: K5 keeps a hidden D at unknown -- the BSOD rung shows in the factors only."""
    stab = stability or {}
    return [_bsod_note(stab)] if raw == "unknown" and stab.get("rung") in STATE_ORDER else []


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
    # state == "unknown" тоже: индекс считается по одной уцелевшей координате,
    # и на цикле «нет видимости» цвет флипался с bad на good.
    if o_val < 40 or state == "unknown":
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
# Orchestrator
# --------------------------------------------------------------------------- #
def compute_health(
    score100_axes: dict,
    bayes: dict,
    trends: dict,
    errchain: Optional[dict],
    cohort: Optional[dict],
    prev_health: Optional[dict],
    *,
    now: Optional[datetime] = None,
    stability: Optional[dict] = None,
) -> HealthVerdict:
    """Assemble (D, R, O) + derivatives; pure (no I/O, no clock; no ``now`` -> no release)."""
    axes = score100_axes if isinstance(score100_axes, dict) else {}
    trends = trends if isinstance(trends, dict) else {}
    o, blind_spots, d, r, r_missing, immature = _coordinates(
        axes, trends, errchain, cohort, prev_health
    )
    o_val = o.value or 0.0
    raw, _label, state_ev, src = _state(d, r, o_val, axes, stability)  # 4
    state, dominant, notes, memory, blind, smart = _held_state(
        raw, [*d.flags, *r.flags], axes, trends, prev_health, now, errchain, src, stability
    )
    index = _index(d.value, None if r.value is None else round(100.0 - r.value, 1), o_val)
    band, reconcile_factors = _reconcile(state, index, o_val)  # 7
    days, reason, kind = horizon(state, trends, prev=prev_health)
    factors = _top([*d.evidence, *r.evidence, *o.evidence], 8) + _gated_bsod(raw, stability)
    factors += _context_factors(dominant, bayes, cohort, trends) + reconcile_factors + notes
    return HealthVerdict(
        damage=replace(d, confidence=_cap(d.confidence, smart.cap)) if smart.cap else d,
        resilience=r,
        observability=o,
        blind_spots=_dedup([*blind_spots, *([_NO_SMART] if blind else []), *smart.spots]),
        state=state,
        state_label=_STATE_LABELS.get(state, ""),
        state_evidence=state_ev + notes,
        index=index,
        band=band,
        confidence=_cap(_confidence(d, r, o_val, immature), smart.cap or "high"),
        dominant=dominant,
        dominant_label=_DOMINANT_LABELS.get(dominant, _DOMINANT_LABELS[None]),
        horizon_days=days,
        horizon_reason=reason,
        horizon_kind=kind,
        action=_action(state, o_val, dominant, days, axes, blind, memory),
        factors=factors,
        missing_evidence=_dedup(list(r_missing) + _confidence_missing(d, r, o_val) + smart.missing),
        raw_state=raw,
        **memory,
    )


def _chain_without_smart(
    r: Coordinate, axes: dict, errchain: Optional[dict], prev: Optional[dict] = None
) -> Coordinate:
    """Storage axis absent or gated: keep the event-log chain stage on R (D8), so the
    stored blob and the next cycle see why. With the axis present its coords carry it.
    H6: a device that never had a disk takes the disk-only stage (an isolated Ntfs 55 is
    «fs_corruption»); a gated axis, a disk that went out of sight, or a SMART-blind cycle
    with the axis present (H1 attrs lost / worst disk dropped) keeps the full one."""
    stage = chain_stage(_ax(axes, "storage_risk"), errchain, prev)
    if stage is None:
        return r
    if _ax_val(axes, "storage_risk") is not None:  # SMART-blind: the full stage, not FS
        r = replace(r, flags=[f for f in r.flags if f != "fs_corruption"])
    elif stage < 2 and ((errchain or {}).get("fs_isolated") or 0) > 0:
        note = {"label": _NOTE_FS, "delta": 0}
        flags = _dedup([*r.flags, "fs_corruption"])
        return replace(r, flags=flags, evidence=[*r.evidence, note])
    if stage < 2:
        return r
    flag = "chain_stage3" if stage >= 3 else "chain_stage2"
    note = {"label": _NOTE_CHAIN, "delta": 0}
    return replace(r, flags=_dedup([*r.flags, flag]), evidence=[*r.evidence, note])


def _coordinates(
    axes: dict, trends: dict, errchain: Optional[dict], cohort: Optional[dict], prev: Optional[dict]
) -> tuple[Coordinate, list, Coordinate, Coordinate, list, bool]:
    """Steps 1-3 (K1): O first, then D, then R with the chain stage the verdict keeps."""
    o, blind_spots = _observability(axes, trends, errchain, cohort)
    d = _damage(axes)
    r, r_missing, immature = _resilience(axes, trends)
    return o, blind_spots, d, _chain_without_smart(r, axes, errchain, prev), r_missing, immature


def _held_state(
    raw: str,
    flags: list,
    axes: dict,
    trends: dict,
    prev: Optional[dict],
    now: Optional[datetime],
    errchain: Optional[dict] = None,
    source: Optional[str] = None,
    stability: Optional[dict] = None,
) -> tuple[str, Optional[str], list, dict, bool, "_NoSmart"]:
    """Hysteresis, dominant, systemic floor, D8 -> (state, dominant, notes, memory, blind)."""
    p = prev or {}
    view = disk_view(_ax(axes, "storage_risk"))
    blind, replaced, vanished = visibility(view, prev)
    cause = blind_cause(view, prev)
    hold = hold_state(raw, prev, now=now, blind=blind, replaced=replaced, cause=cause)
    dominant, systemic = _dominant(axes, trends)
    disk_flag = bool((_H3_FLAGS | _H4_FLAGS) & set(flags))
    dominant = "storage" if disk_flag else dominant  # a live h3/h4 flag: the disk (D7, H8)
    from_hold = hold.held  # the floor lifting a held level does not make it fresh (R5)
    floor = systemic and hold.state != "unknown" and STATE_ORDER.get(hold.state, 4) < 2
    state = "h2" if floor else hold.state  # systemic floor
    src = "systemic_floor" if floor else ("hold" if from_hold else source)
    dominant = _source_dominant(src, dominant, axes, trends, prev)  # H8
    dominant, mechanism = held_mechanism(dominant, systemic, disk_flag, from_hold, prev)
    held = p.get("held_source") if from_hold else src  # what set the shown level (H8)
    seen = damage_seen(flags, prev, replaced)  # in the episode: blind cycles, 197 toggles
    basis = "flag:damage_present" if held in _CHECK_SOURCES and seen else held
    last_known = state if state != "unknown" else p.get("last_known_state")
    if state == "h0" and (view.gated or (blind and not view.present)):  # D8: never "healthy"
        state = "unknown"
    memory = {
        "last_known_state": last_known,
        "below_h": hold.below_h,
        "below_seen_at": hold.below_seen_at,
        "held_dominant": mechanism,
        "blind_cause": cause,
        "unknown_pause_used": hold.pause_used,
        "disk_replaced": replaced,
        "cause_class": _cause_class(basis, dominant, from_hold, p, errchain, stability, axes),
        "rung_source": src,
        "held_source": basis,
        **identity(view, prev, blind),
    }
    smart = _no_smart(state, view, axes, errchain, prev, memory["had_disk"])
    notes = hold_notes(hold, blind, vanished, cause=cause) + smart.notes
    return smart.state, dominant, notes, memory, blind, smart


class _NoSmart(NamedTuple):
    state: str
    notes: list
    missing: list
    spots: list
    cap: Optional[str]  # confidence ceiling


_SOURCE_DOMINANT = {
    "rloss:storage": "storage",
    "heavy_storage": "storage",
    "d:storage": "storage",
    "rloss:aging": "aging",
    "d:os": "os",
    "systemic_floor": "systemic",
    "fill_floor": "disk_fill",  # H11
    "bsod_days": "os",  # H13
}
# an h2 by retries / a corroborated latency tail / remap masking without confirmed
# damage asks to CHECK the disk (H8); spare_depleting is a forecast (ETA <= 90 d to the
# vendor threshold) and keeps the storage action like the h0 spare forecast
_CHECK_SOURCES = frozenset(
    f"flag:{f}" for f in ("early_events", "remap_masking", "tail_ratio_worsening")
)


def _source_dominant(
    src: Optional[str],
    background: Optional[str],
    axes: dict,
    trends: dict,
    prev: Optional[dict] = None,
) -> Optional[str]:
    """H8: the dominant is the source of the rung. A hold keeps the background here
    (held_mechanism names the held one); h0 / unknown keep it unless a forecast is
    shown -- then its basis, like the trajectory driver."""
    if src == "hold":
        return background
    if src is None:
        key = forecast_key(trends, prev)  # the held bucket horizon() shows (H7)
        return _TRAJECTORY_DRIVERS[key] if key else background
    if src == "rloss:trajectory":
        return _trajectory_driver(trends, _ax_val(axes, "trajectory_risk") or 0.0)
    if src.startswith("flag:"):
        name = src[5:]
        return "os" if name == "fs_corruption" else "aging" if name in _H1_FLAGS else "storage"
    return _SOURCE_DOMINANT.get(src, background)


def _cause_class(
    basis: Optional[str],
    dominant: Optional[str],
    from_hold: bool,
    prev: dict,
    errchain: Optional[dict],
    stability: Optional[dict],
    axes: Optional[dict] = None,
) -> Optional[str]:
    """H6/H13/H14: the cause of a file-system, a BSOD or a disk level (never moves the
    state); a held level keeps the cause it was set with."""
    fs = basis == "flag:fs_corruption" and dominant == "os"
    kept = from_hold and prev.get("cause_class") == "fleet_likely"  # H14: a hold keeps it
    disk = dominant == "storage" and basis is not None and (kept or _cohort_disk(axes, errchain))
    if not (fs or disk or basis == "bsod_days"):
        return None
    if from_hold and prev.get("cause_class"):
        return prev["cause_class"]
    if disk:
        return "fleet_likely"
    return _fs_cause(errchain) if fs else (stability or {}).get("cause_class") or "unknown"


_COHORT_DISK_MIN = 3  # H14 (R4b): machines of the model with disk errors in 30 days ...
_COHORT_DISK_SHARE = 0.30  # ... and their share of the cohort
_FLEET_DISK_ACTION = (
    "снять образ данных; проверить модель и прошивку накопителя на когорте — начать с машин "
    "с наибольшим числом ошибок"
)


def _cohort_disk(axes: Optional[dict], errchain: Optional[dict]) -> bool:
    """H14: this machine logs disk errors 7/51/153/129 (an Ntfs 55 is none -- H6) and
    >= 3 machines / >= 30 % of its model do too."""
    if not (errchain or {}).get("disk_errors"):
        return False
    lin = _ax(axes or {}, "fleet_anomaly_risk").get("source_lineage") or {}
    n, share = lin.get("cohort_disk_err_n") or 0, lin.get("cohort_disk_err_pct") or 0.0
    return n >= _COHORT_DISK_MIN and share >= _COHORT_DISK_SHARE


def _fs_cause(errchain: Optional[dict]) -> str:
    """H6 cause of a file-system corruption: a power loss in the 24 h before, or none."""
    return "power_likely" if (errchain or {}).get("fs_power_likely") else "unknown"


def _no_smart(
    state: str,
    view: Any,
    axes: dict,
    errchain: Optional[dict],
    prev: Optional[dict],
    had_disk: bool,
) -> _NoSmart:
    """H5 + H12, after the hold: a phantom (axis seen, no health data ever) keeps its O
    but is low-confidence; a FINAL h0 without health data -- phantom, or a device that
    never had a disk -- stands only on the events channel plus the OS axis, else unknown.
    A held h1+ is untouched."""
    ghost = phantom(view, prev)
    never = not view.present and not view.gated and not had_disk
    missing = [PHANTOM_MISSING] if ghost else []
    spots, cap = ([_NO_HEALTH_DATA], "low") if ghost else ([], None)
    if state != "h0" or not (ghost or never):
        return _NoSmart(state, [], missing, spots, cap)
    checks = (
        (_events_live(errchain), "нет анализа событий"),
        (_ax_val(axes, "os_degradation_risk") is not None, "нет оценки ОС"),
    )
    lacking = [msg for live, msg in checks if not live]
    if lacking:
        missing = [*missing, f"{_MSG_INSUFFICIENT}, {', '.join(lacking)}"]
        return _NoSmart("unknown", [], missing, spots, cap)
    note = {"label": _NOTE_AVAILABLE, "delta": 0}
    return _NoSmart("h0", [note], missing, spots, cap or "medium")


def _action(
    state: str,
    o_val: float,
    dominant: Optional[str],
    days: Optional[int],
    axes: dict,
    blind: bool,
    memory: Optional[dict] = None,
) -> str:
    """State/action consistency (D7): a healthy verdict without a horizon never asks
    to replace a disk; operational dominants and any forecast keep their own action.
    A blind h0 (SMART lost; §5.16 keeps the state) is no all-clear either."""
    if state == "unknown" or o_val < 40:
        return _BLIND_ACTION
    source, cause = (memory or {}).get("held_source"), (memory or {}).get("cause_class")
    disk_cohort = cause == "fleet_likely" and source != "bsod_days"  # a BSOD hold is H13
    if disk_cohort and dominant == "storage" and state != "h0":  # H14
        return _FLEET_DISK_ACTION
    if source == "bsod_days" and dominant == "os" and state in ("h1", "h2", "h3"):
        return BSOD_ACTIONS.get(cause or "unknown", BSOD_ACTIONS["unknown"])  # H13
    if state in ("h1", "h2"):  # h1: a held level one step down keeps the action
        if source == "flag:fs_corruption" and dominant == "os":
            return _FS_ACTION  # H6: the file system, not the medium
        if source in _CHECK_SOURCES and dominant == "storage":
            return _CHECK_ACTION  # H8: readings without confirmed damage
    if state == "h0" and days is None and dominant in (None, "storage", "os"):
        if blind:
            return _BLIND_ACTION
        bad = dominant == "storage" and _ax(axes, "storage_risk").get("band") == "bad"
        return _CHECK_ACTION if bad else _OBSERVE_ACTION
    return _ACTIONS.get(dominant, _ACTIONS[None])


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
    classes = (bayes or {}).get("classes") or []
    level = next(
        (c.get("level") for c in classes if isinstance(c, dict) and c.get("name") == top), None
    )
    if top and top != dominant and level not in (None, "low"):  # a low class is no signal
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


def state_label_for(state: Optional[str]) -> str:
    """Public accessor for _STATE_LABELS -- mirrors action_for so read-side consumers
    never reach into a module-private dict directly."""
    return _STATE_LABELS.get(state or "unknown", _STATE_LABELS["unknown"])


def dominant_label_for(dominant: Optional[str]) -> str:
    """Public accessor for _DOMINANT_LABELS -- mirrors action_for."""
    return _DOMINANT_LABELS.get(dominant, _DOMINANT_LABELS[None])


# One literal for health_staleness and apply_health_staleness (server.api's
# _with_health_staleness delegates to the latter instead of keeping a copy).
_STALE_UNKNOWN_MSG = "проверка недостоверна: данные старше 10 дней"


def health_staleness(score_ts: str, now: datetime) -> Optional[str]:
    """Score-age staleness, applied by callers (API/pages), never inside compute_health
    (no timestamp input): None when fresh (<= 3 days) or unparseable; «данные устарели
    (N дн.)» for 3-10 days (the caller caps confidence at low); _STALE_UNKNOWN_MSG past
    10 days, the distinguishable value the caller treats as an UNKNOWN verdict."""
    ts = _parse_dt(score_ts)
    if ts is None:
        return None
    age = now - ts
    if age > timedelta(days=10):
        return _STALE_UNKNOWN_MSG
    if age > timedelta(days=3):
        return f"данные устарели ({age.days} дн.)"
    return None


def apply_health_staleness(health: dict, score_ts: Optional[str], now: datetime) -> dict:
    """Read-side staleness overlay (T6.1 step 8 / T7.2) as a new dict, ``health`` never
    mutated: >10d -> state/band/confidence "unknown" + a blind_spots note; >3d ->
    confidence "low" + a missing_evidence note; else an unchanged copy. The API
    delegates here too, so the fleet page, the API and the device page agree."""
    out = dict(health)
    if not score_ts:
        return out
    msg = health_staleness(score_ts, now)
    if msg is None:
        return out
    if msg == _STALE_UNKNOWN_MSG:
        out["state"] = "unknown"
        out["band"] = "unknown"
        out["confidence"] = "unknown"
        out["blind_spots"] = [*(out.get("blind_spots") or []), msg]
    else:
        out["confidence"] = "low"
        out["missing_evidence"] = [*(out.get("missing_evidence") or []), msg]
    return out


def _parse_dt(value: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
