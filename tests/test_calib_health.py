"""Health-verdict calibration (spec 2026-10-09, D2-D8): literature ladder, honest
horizon, observed-time hysteresis with SMART-blindness freeze and disk identity,
systemic = degradation mechanisms only, state/action consistency.

Plain-dict fixtures shaped like the persisted ``risk_block`` (same style as
tests/test_health.py). Operator text is pinned verbatim; machine values in English.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pytest
from server.analytics.health import _ACTIONS, _BLIND_ACTION, compute_health
from server.analytics.software_aging import compute_software_aging_risk
from server.analytics.trends import compute_trends, trend_to_dict
from server.scoring.score100 import band_for_risk_score, score_to_dict

NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
OBSERVE = "наблюдать — повреждений не выявлено, действий не требуется"
CHECK = "проверить накопитель — повышенные показатели без подтверждённых повреждений"
REACTION = "срок реакции, не прогноз отказа"
IMMATURE = "оценка устойчивости незрелая"
BLIND_NOTE = "SMART временно недоступен — снижение не подтверждается"
VANISHED_NOTE = "диск пропал из перечня SMART — снижение подтверждается по времени"
CHAIN_NOTE = "цепочка ошибок диска по журналу событий (SMART недоступен)"
FILL_ACTION = "освободить место — под угрозой обновления Windows"
NO_SMART = "SMART недоступен"
NO_PERF = "нет телеметрии производительности"
GATED = {"storage_domain": "unknown"}
# A live event channel (one counted event, stage 0) -- keeps observability >= 40
# on cycles where the storage axis is hidden or absent.
LIVE_CHAIN = {
    "stage": 0,
    "burstiness": None,
    "recurrent_weeks": 0,
    "counts": {"early": 1, "damage": 0, "crash": 0, "app_hang": 0},
    "factors": [],
}


# --------------------------------------------------------------------------- #
# dict fixture builders
# --------------------------------------------------------------------------- #
def _lineage(
    ids: list,
    *,
    attr: Optional[list] = None,
    worst: Optional[str] = None,
    inventory: Optional[list] = None,
) -> dict:
    lineage = {
        "worst_disk": "WD Blue",
        "worst_disk_id": worst or ids[0],
        "disk_ids": list(ids),
        "attr_disk_ids": list(ids[:1] if attr is None else attr),
    }
    if inventory is not None:  # pipeline adds the Get-PhysicalDisk serial hashes
        lineage["inventory_disk_ids"] = list(inventory)
    return lineage


LIN = _lineage(["hA", "WD Blue"])


def _axis(value: Optional[float], *, coords: Optional[dict] = None, lineage=None) -> dict:
    d: dict[str, Any] = {
        "value": value,
        "direction": "higher_is_worse",
        "band": band_for_risk_score(value),
        "confidence": "high",
        "factors": [],
        "missing_evidence": [],
        "source_lineage": dict(lineage or {}),
        "reason": "",
    }
    if coords is not None:
        d["coords"] = coords
    return d


def _st(value: float = 0.0, *, damage=0.0, rloss=0.0, flags=(), lineage=LIN) -> dict:
    coords = {"damage": damage, "resilience_loss": rloss, "flags": list(flags)}
    return _axis(value, coords=coords, lineage=lineage)


def _no_storage(lineage: dict) -> dict:
    """Storage axis value None (gated or no SMART); OS + aging stay live."""
    return {
        "storage_risk": _axis(None, lineage=lineage),
        "os_degradation_risk": _axis(0.0),
        "software_aging_risk": _axis(0.0, coords={"flags": []}),
    }


def _trend(direction: str = "worsening", *, n_points: int = 8, eta=None) -> dict:
    return {
        "metric": "m",
        "n_points": n_points,
        "current": None,
        "slope_per_day": 1.0,
        "eta_days": eta,
        "target_date": None,
        "direction": direction,
        "reason": "",
        "slope_recent": 1.0,
        "accelerating": False,
        "span_days": 10.0,
    }


MATURE = {"nvme_spare": _trend("flat")}


def _prev(state: str, **over: Any) -> dict:
    base = {
        "state": state,
        "last_known_state": state,
        "dominant": "storage",
        "worst_disk": "WD Blue",
        "worst_disk_id": "hA",
        "disk_ids": ["WD Blue", "hA"],
        "attr_disk_ids": ["hA"],
    }
    return {**base, **over}


def _seen(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def _call(axes, *, trends=None, prev=None, now=None, errchain=None, bayes=None):
    clock = {} if now is None else {"now": now}
    return compute_health(
        axes,
        bayes or {"classes": [], "top": None, "overall": 0.0},
        {**MATURE, **(trends or {})},
        errchain,
        None,
        prev,
        **clock,
    )


def _labels(items: list) -> list:
    return [i.get("label") for i in items]


# --------------------------------------------------------------------------- #
# Ladder (D4)
# --------------------------------------------------------------------------- #
def test_stable_pending_gt10_alone_is_h3_with_reaction_window() -> None:
    v = _call({"storage_risk": _st(60, damage=60, flags=["pending_gt10"])})
    assert v.state == "h3"
    assert (v.horizon_days, v.horizon_kind) == (30, "state")
    assert REACTION in v.horizon_reason
    assert v.horizon_reason == (
        "ускоренная деградация — действовать в течение 30 дней (срок реакции, не прогноз отказа)"
    )


def test_uncorrectable_198_alone_is_h3() -> None:
    assert _call({"storage_risk": _st(50, damage=50, flags=["uncorrectable_198"])}).state == "h3"


def test_cw_spare_and_spare_below_threshold_count_once() -> None:
    flags = ["cw_spare", "spare_below_threshold"]  # one mechanism, two names
    assert _call({"storage_risk": _st(50, flags=flags)}).state == "h3"


def test_cw_reliability_alone_is_h3() -> None:
    assert _call({"storage_risk": _st(60, damage=60, flags=["cw_reliability"])}).state == "h3"


def test_two_hard_media_mechanisms_are_h4() -> None:
    flags = ["pending_gt10", "uncorrectable_198"]
    v = _call({"storage_risk": _st(60, damage=60, flags=flags)})
    assert v.state == "h4"
    assert v.horizon_reason == (
        "предотказ — действовать в течение 7 дней (срок реакции, не прогноз отказа)"
    )


def test_hard_media_with_growth_is_h4() -> None:
    cases = (
        ["pending_gt10", "recurrence"],
        ["uncorrectable_198", "accel"],
        # G4: NVMe spare below threshold with growing media errors
        ["spare_below_threshold", "cw_spare", "recurrence"],
    )
    for flags in cases:
        assert _call({"storage_risk": _st(60, damage=60, flags=flags)}).state == "h4", flags


def test_spare_collapse_is_h4() -> None:
    flags = ["spare_below_threshold", "cw_spare", "spare_consumed"]
    v = _call({"storage_risk": _st(60, rloss=60, flags=flags)})
    assert v.state == "h4"
    assert v.horizon_days == 7


def test_spare_consumed_alone_changes_nothing() -> None:
    assert _call({"storage_risk": _st(flags=["spare_consumed"])}).state == "h0"


def test_predict_fail_readonly_chain_stage3_are_h4() -> None:
    for flag in ("predict_fail", "cw_readonly", "chain_stage3"):
        assert _call({"storage_risk": _st(70, damage=70, flags=[flag])}).state == "h4", flag


def test_heavy_stable_damage_is_h2() -> None:
    for damage in (40.0, 45.0):  # 40 is the rung's own boundary: 187 > 0, attr5 > 15
        v = _call({"storage_risk": _st(damage, damage=damage, flags=["damage_present"])})
        assert v.state == "h2", damage
        assert (v.horizon_days, v.horizon_kind) == (90, "state")
        assert v.horizon_reason == (
            "компенсация — действовать в течение 90 дней (срок реакции, не прогноз отказа)"
        )


def test_d30_damage_stays_h1() -> None:
    v = _call({"storage_risk": _st(30, damage=30, flags=["damage_present"])})
    assert v.state == "h1"
    assert v.horizon_days is None


def test_os_degradation_never_sizes_heavy_damage() -> None:
    """«D >= 40 при damage_present» is the storage channel's own damage: an OS
    degradation must not lift a small stable storage defect (attr5 1-15) to h2."""
    axes = {
        "storage_risk": _st(30, damage=30, flags=["damage_present"]),
        "os_degradation_risk": _axis(70),  # 70 * 0.6 = D 42 from the OS channel
    }
    v = _call(axes)
    assert v.damage.value == 42
    assert v.state == "h1"


# --------------------------------------------------------------------------- #
# Tail latency and spare (D4)
# --------------------------------------------------------------------------- #
def test_tail_alone_never_above_h1() -> None:
    v = _call({"storage_risk": _st()}, trends={"disk_tail_ratio": _trend()})
    assert "tail_ratio_worsening" not in v.resilience.flags
    assert v.resilience.value == 80  # the +20 surcharge stays
    assert v.state == "h0"


def test_tail_with_stable_damage_only_is_not_flagged() -> None:
    axes = {"storage_risk": _st(30, damage=30, flags=["damage_present"])}
    v = _call(axes, trends={"disk_tail_ratio": _trend()})
    assert "tail_ratio_worsening" not in v.resilience.flags
    assert v.state == "h1"


def test_tail_corroborated_by_timeouts_is_h2() -> None:
    v = _call({"storage_risk": _st(flags=["cmd_timeouts"])}, trends={"disk_tail_ratio": _trend()})
    assert "tail_ratio_worsening" in v.resilience.flags
    assert v.state == "h2"


def test_spare_depleting_needs_eta_le_90() -> None:
    far = _call({"storage_risk": _st()}, trends={"nvme_spare": _trend(eta=200)})
    assert "spare_depleting" not in far.resilience.flags
    assert far.resilience.value == 100
    near = _call({"storage_risk": _st()}, trends={"nvme_spare": _trend(eta=60)})
    assert "spare_depleting" in near.resilience.flags
    assert near.resilience.value == 75
    assert near.state == "h2"
    edge = _call({"storage_risk": _st()}, trends={"nvme_spare": _trend(eta=90)})
    assert "spare_depleting" in edge.resilience.flags  # the 90-day boundary is inclusive


def test_spare_surcharge_not_doubled_with_compensation_breach() -> None:
    axes = {"storage_risk": _st(25, rloss=25, flags=["compensation_breach"])}
    v = _call(axes, trends={"nvme_spare": _trend(eta=60)})
    assert v.resilience.value == 75  # one spare trend counted once
    assert "spare_depleting" in v.resilience.flags


# --------------------------------------------------------------------------- #
# Software aging (D2)
# --------------------------------------------------------------------------- #
def test_aging_leak_caps_at_h1_without_horizon() -> None:
    aging = _axis(75, coords={"flags": ["aging_leak", "aging_accelerating"]})
    v = _call({"storage_risk": _st(), "software_aging_risk": aging})
    assert v.state == "h1"
    assert v.resilience.value == 61
    assert v.horizon_days is None
    assert v.dominant == "aging"
    assert v.action == _ACTIONS["aging"]


def _office_pc_heartbeats(days: int) -> list:
    """Boots at 08:00 and shuts down after ~13 h: 4 h heartbeats at uptime 0.2 / 4.2 /
    8.2 / 12.2 -- under 4 usable rows per session for the calibrated aging engine (D2)."""
    t0 = datetime(2026, 9, 1, 8, tzinfo=timezone.utc)
    rows = [
        {
            "received_at": (t0 + timedelta(days=day, hours=up)).isoformat(),
            "uptime_hours": up,
            "user_present": k > 0,
            "handle_count_total": 60000,
            "mem_avail_mb": 8000,
            "free_space_pct": 55.0,
            "cpu_perf_pct": 99.0,
        }
        for day in range(days)
        for k, up in enumerate((0.2, 4.2, 8.2, 12.2))
    ]
    return rows[::-1]  # newest-first, as db.get_recent_heartbeats returns them


def test_short_sessions_keep_performance_telemetry_live() -> None:
    """Review R2: the observability liveness channel reads heartbeats, not just the aging
    value -- the aging engine is UNKNOWN for a daily-shutdown office PC whose heartbeats
    arrive. No heartbeat at all, or an untrusted identity, is still no telemetry."""
    for hb, live in ((_office_pc_heartbeats(10), True), ([], False)):
        aging = compute_software_aging_risk(hb)
        assert aging.value is None  # the engine's own short-session UNKNOWN
        axes = {"storage_risk": _st(), "software_aging_risk": score_to_dict(aging)}
        trends = {k: trend_to_dict(t) for k, t in compute_trends([], hb).items()}
        v = _call(axes, trends=trends)
        assert (NO_PERF not in v.blind_spots) is live, live
        untrusted = compute_software_aging_risk(hb, device_trust="untrusted")
        axes["software_aging_risk"] = score_to_dict(untrusted)
        assert NO_PERF in _call(axes, trends=trends).blind_spots


def test_idle_heartbeats_without_free_space_keep_performance_telemetry_live() -> None:
    """Review 3c: throttle points are loaded-only since H3, so they no longer witness
    heartbeats. An idle office PC whose free-space query fails still sends heartbeats --
    a real event-chain h4 without SMART must not turn into «нет видимости»."""
    hb = [{k: v for k, v in r.items() if k != "free_space_pct"} for r in _office_pc_heartbeats(10)]
    aging = compute_software_aging_risk(hb)
    assert aging.value is None  # short sessions: the aging engine is UNKNOWN
    trends = {k: trend_to_dict(t) for k, t in compute_trends([], hb).items()}
    assert not trends["throttle"]["n_points"]  # idle: no loaded sample (H3)
    axes = {"software_aging_risk": score_to_dict(aging), "os_degradation_risk": _axis(5.0)}
    v = _call(axes, trends=trends, errchain={**LIVE_CHAIN, "stage": 3})
    assert NO_PERF not in v.blind_spots and v.state == "h4"
    untrusted = compute_software_aging_risk(hb, device_trust="untrusted")
    axes["software_aging_risk"] = score_to_dict(untrusted)
    assert NO_PERF in _call(axes, trends=trends).blind_spots


# --------------------------------------------------------------------------- #
# Hysteresis by observed time (D3)
# --------------------------------------------------------------------------- #
H1_AXES = {"storage_risk": _st(20, damage=20)}  # raw state h1


def test_release_after_48_observed_hours_steps_one_level() -> None:
    v = _call(H1_AXES, prev=_prev("h3", below_h=46.0, below_seen_at=_seen(4)), now=NOW)
    assert v.state == "h2"  # exactly one level, not straight to h1
    assert v.below_h == 0.0
    assert v.below_seen_at == NOW.isoformat()
    # the last step lands on the raw state: nothing is held any more, the clock clears
    last = _call(H1_AXES, prev=_prev("h2", below_h=46.0, below_seen_at=_seen(4)), now=NOW)
    assert (last.state, last.below_h, last.below_seen_at) == ("h1", None, None)


def test_release_at_exactly_48_observed_hours() -> None:
    v = _call(H1_AXES, prev=_prev("h3", below_h=44.0, below_seen_at=_seen(4)), now=NOW)
    assert (v.state, v.below_h) == ("h2", 0.0)  # 44 + 4 = 48 h releases one level


def test_release_holds_before_48h() -> None:
    v = _call(H1_AXES, prev=_prev("h3", below_h=40.0, below_seen_at=_seen(4)), now=NOW)
    assert v.state == "h3"
    assert v.below_h == 44.0


def test_gap_counts_at_most_6h() -> None:
    v = _call(H1_AXES, prev=_prev("h3", below_h=40.0, below_seen_at=_seen(60)), now=NOW)
    assert v.state == "h3"
    assert v.below_h == 46.0  # a weekend without telemetry is not observation


def test_raw_back_up_resets_clock() -> None:
    axes = {"storage_risk": _st(40, damage=40, flags=["accel"])}  # raw h3
    v = _call(axes, prev=_prev("h3", below_h=40.0, below_seen_at=_seen(4)), now=NOW)
    assert v.state == "h3"
    assert v.below_h is None and v.below_seen_at is None


def test_now_none_never_releases() -> None:
    prev = _prev("h3", below_h=100.0, below_seen_at=_seen(4))
    v = _call(H1_AXES, prev=prev)
    assert v.state == "h3"
    assert (v.below_h, v.below_seen_at) == (100.0, prev["below_seen_at"])


def test_reboot_restores_does_not_bypass_hold() -> None:
    aging = _axis(0.0, coords={"flags": ["reboot_restores"]})
    v = _call({"storage_risk": _st(), "software_aging_risk": aging}, prev={"state": "h2"}, now=NOW)
    assert v.state == "h2"


# --------------------------------------------------------------------------- #
# Storage blindness and disk identity (D3, D8)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("source", "cause", "note"),
    [
        # H1 (owner decision 2): no fresh historical (STALE) is offline -> pause, not reset
        (
            {"state": "stale", "collector_status": "ok", "historical_ran": True},
            "offline",
            "свежих данных хранилища нет — отсчёт снижения на паузе",
        ),
        # no trust hint: unknown -> the first episode of the window pauses too
        (None, "unknown", "видимость хранилища потеряна, причина не установлена — отсчёт на паузе"),
    ],
)
def test_gated_cycle_holds_and_pauses_the_clock(
    source: Optional[dict], cause: str, note: str
) -> None:
    # was test_gated_cycle_holds_and_resets_clock: the reset is now only for a real SMART
    # loss (test_attrs_lost_freezes_release); spec H1 / F-01, D01-style stuck holds (R3)
    lineage = {**GATED, **({"storage_source": source} if source else {})}
    prev = _prev("h4", below_h=30.0, below_seen_at=_seen(4))
    v = _call(_no_storage(lineage), prev=prev, now=NOW, errchain=LIVE_CHAIN)
    assert v.state == "h4"
    assert v.below_h == 30.0 and v.below_seen_at is None  # paused: the next cycle adds 0 h
    assert (v.blind_cause, v.unknown_pause_used) == (cause, cause == "unknown")
    assert (v.worst_disk_id, v.disk_ids, v.attr_disk_ids) == ("hA", ["WD Blue", "hA"], ["hA"])
    assert note in _labels(v.state_evidence) and BLIND_NOTE not in _labels(v.state_evidence)
    # nothing to hold: a hidden storage axis still never certifies "healthy"
    fresh = _call(_no_storage(GATED), now=NOW, errchain=LIVE_CHAIN)
    assert fresh.state == "unknown"
    assert fresh.action == _BLIND_ACTION


def test_smart_less_cycle_after_disk_never_reads_h0() -> None:
    held = _call(_no_storage({}), prev=_prev("h4"), now=NOW, errchain=LIVE_CHAIN)
    assert held.state == "h4"
    lost = _call(_no_storage({}), prev=_prev("h0"), now=NOW, errchain=LIVE_CHAIN)
    assert lost.state == "unknown"
    assert lost.action == _BLIND_ACTION


def test_attrs_lost_freezes_release() -> None:
    axes = {"storage_risk": _st(20, damage=20, lineage=_lineage(["hA", "WD"], attr=[]))}
    prev = _prev("h3", disk_ids=["hA", "WD"], below_h=47.9, below_seen_at=_seen(4))
    v = _call(axes, prev=prev, now=NOW)
    assert v.state == "h3"
    assert v.below_h is None
    assert BLIND_NOTE in _labels(v.state_evidence)
    # carried forward: the freeze lasts until the attributes come back
    assert v.attr_disk_ids == ["hA"]


def test_attrs_lost_at_h0_never_reads_unknown() -> None:
    """Spec §5.16: an attrs-lost cycle freezes the release but keeps h0 -- otherwise
    h0 and «нет видимости» would flip on every SMART script timeout. Review R2: the
    action is no all-clear then, and the blind spot names SMART. Review R3: an h0
    action has no colon -- Zabbix joins "label: action" (D7)."""
    axes = {"storage_risk": _st(lineage=_lineage(["hA", "WD Blue"], attr=[]))}
    for prev in (_prev("h0"), _prev("h0", had_disk=True, dominant=None)):
        v = _call(axes, prev=prev, now=NOW)
        assert (v.state, v.band) == ("h0", "good")
        assert v.action == _BLIND_ACTION
        assert ":" not in v.action
        assert NO_SMART in v.blind_spots
        assert v.attr_disk_ids == ["hA"]  # carried: the next cycle still sees the loss
    seen = _call({"storage_risk": _st()}, prev=_prev("h0"), now=NOW)  # attributes are back
    assert (seen.action, NO_SMART in seen.blind_spots) == (OBSERVE, False)


def test_attrs_lost_wins_over_replacement() -> None:
    """Instruction 6d order: a blind cycle freezes even when the worst disk is replaced
    (hA left the inventory too, so without the lost attributes this is a replacement)."""
    ids = ["hC", "WD2", "hB", "Samsung"]  # hA gone, hC new, hB listed without attributes
    axes = {"storage_risk": _st(lineage=_lineage(ids, attr=["hC"], inventory=["hC", "hB"]))}
    prev = _prev("h3", disk_ids=["hA", "WD", "hB", "Samsung"], attr_disk_ids=["hA", "hB"])
    v = _call(axes, prev=prev, now=NOW)
    assert v.state == "h3"
    assert BLIND_NOTE in _labels(v.state_evidence)


def test_old_smart_less_storage_row_stays_frozen() -> None:
    """Rollout: a pre-calibration row latched on storage while SMART was already gone
    (worst_disk None, no disk ids). The persisted had_disk marker keeps every later
    SMART-less cycle blind, so the old verdict is frozen, not released to «здоров»."""
    prev = {"state": "h4", "last_known_state": "h4", "dominant": "storage", "worst_disk": None}
    for cycle in range(60):  # 10 days of 4 h cycles
        now = NOW + timedelta(hours=4 * cycle)
        v = _call(_no_storage({}), prev=prev, now=now, errchain=LIVE_CHAIN)
        assert v.state == "h4", cycle
        prev = {**dataclasses.asdict(v), "worst_disk": None}  # as pipeline stores it
    assert prev["had_disk"] is True


def test_blind_cycle_without_damage_channel_keeps_held_state() -> None:
    """D8: a held h1+ stays visible on a blind cycle even when the OS axis is gone too
    (raw 'unknown'); with nothing held the K5 'unknown' gate stands."""
    for lineage in (GATED, {}):
        axes = {**_no_storage(lineage), "os_degradation_risk": _axis(None)}
        held = _call(axes, prev=_prev("h4"), now=NOW, errchain=LIVE_CHAIN)
        assert held.state == "h4", lineage
        assert (held.below_h, held.below_seen_at) == (None, None)  # frozen, clock reset
        assert held.action == _ACTIONS["storage"]
        fresh = _call(axes, now=NOW, errchain=LIVE_CHAIN)
        assert (fresh.state, fresh.action) == ("unknown", _BLIND_ACTION)


def test_vm_without_smart_stays_h0() -> None:
    for prev in (None, {"state": "h0", "last_known_state": "h0"}):
        v = _call(_no_storage({}), prev=prev, now=NOW, errchain=LIVE_CHAIN)
        assert v.state == "h0", prev


def test_same_model_swap_resets_by_serial() -> None:
    axes = {"storage_risk": _st(lineage=_lineage(["hC", "WD Blue"], inventory=["hC"]))}
    v = _call(axes, prev=_prev("h4"), now=NOW)
    assert v.state == "h0"
    assert v.worst_disk_id == "hC"


def test_vanished_disk_does_not_reset() -> None:
    """G4: the dying data disk leaves the SMART list and the inventory -- removed, not a
    replacement: the time release with its note, no reset."""
    axes = {"storage_risk": _st(lineage=_lineage(["hB", "Samsung"], inventory=["hB"]))}
    v = _call(axes, prev=_prev("h4", disk_ids=["hA", "WD", "hB", "Samsung"]), now=NOW)
    assert v.state == "h4"
    assert VANISHED_NOTE in _labels(v.state_evidence)


def test_dropped_disk_still_in_inventory_is_blind() -> None:
    """G4, review R2: the agent drops a disk from its storage rows once it stops answering
    the reliability/SMART query, while Get-PhysicalDisk still lists it. That is blindness,
    not a vanished disk: the hold freezes and the dying disk's identity is carried. No
    inventory list at all is blind too (UNKNOWN over false confidence)."""
    prev = _prev(
        "h4",
        disk_ids=["hA", "WD", "hB", "Samsung"],
        attr_disk_ids=["hA", "hB"],
        below_h=40.0,
        below_seen_at=_seen(4),
    )
    for inventory in (["hA", "hB"], None, []):
        lineage = _lineage(["hB", "Samsung"], attr=["hB"], worst="hB", inventory=inventory)
        v = _call({"storage_risk": _st(lineage=lineage)}, prev=prev, now=NOW)
        assert v.state == "h4", inventory
        assert (v.below_h, v.below_seen_at) == (None, None)  # frozen, the clock reset
        assert (v.worst_disk_id, v.disk_ids) == ("hA", ["Samsung", "WD", "hA", "hB"])
        assert BLIND_NOTE in _labels(v.state_evidence)
        assert VANISHED_NOTE not in _labels(v.state_evidence)
        assert NO_SMART in v.blind_spots
    # a replacement needs the old disk gone from the inventory as well
    swap = _lineage(["hC", "WD Blue"], inventory=["hA", "hC"])
    assert _call({"storage_risk": _st(lineage=swap)}, prev=_prev("h4"), now=NOW).state == "h4"


def test_old_row_without_ids_uses_name_rule() -> None:
    prev = {"state": "h3", "last_known_state": "h3", "worst_disk": "WD Blue"}
    same = _call({"storage_risk": _st(lineage=_lineage(["hA", "WD Blue"]))}, prev=prev, now=NOW)
    assert same.state == "h3"
    new = _lineage(["hB", "Other"], inventory=["hB"])
    other = _call({"storage_risk": _st(lineage=new)}, prev=prev, now=NOW)
    assert other.state == "h0"


def test_worst_pick_flip_between_present_disks_does_not_reset() -> None:
    ids = ["hA", "WD", "hB", "Samsung"]
    axes = {"storage_risk": _st(lineage=_lineage(ids, attr=["hA", "hB"], worst="hB"))}
    v = _call(axes, prev=_prev("h3", disk_ids=ids, attr_disk_ids=["hA", "hB"]), now=NOW)
    assert v.state == "h3"


# --------------------------------------------------------------------------- #
# Chain without SMART, maturity, systemic, dominant
# --------------------------------------------------------------------------- #
def test_chain_flags_kept_without_smart() -> None:
    axes = {**_no_storage({}), "os_degradation_risk": _axis(10)}
    counts = {"early": 0, "damage": 2, "crash": 1, "app_hang": 0}
    for stage, flag, state in ((3, "chain_stage3", "h4"), (2, "chain_stage2", "h3")):
        v = _call(axes, errchain={**LIVE_CHAIN, "stage": stage, "counts": counts})
        assert v.state == state
        assert flag in v.resilience.flags
        assert CHAIN_NOTE in _labels(v.resilience.evidence)
    quiet = _call(axes, errchain={**LIVE_CHAIN, "stage": 1, "counts": counts})
    assert not {"chain_stage2", "chain_stage3"} & set(quiet.resilience.flags)
    assert quiet.state == "h0"


def test_chain_only_verdict_names_storage() -> None:
    """D7: a chain verdict without a SMART view names the storage mechanism and its
    action, so the Zabbix reason never reads «не определено» next to h3/h4."""
    chain = {**LIVE_CHAIN, "counts": {"early": 0, "damage": 2, "crash": 1, "app_hang": 0}}
    for lineage in ({}, GATED):
        axes = {**_no_storage(lineage), "os_degradation_risk": _axis(10)}
        for stage, state in ((2, "h3"), (3, "h4")):
            for prev in (None, _prev(state)):  # raw == prev: nothing held to borrow from
                v = _call(axes, prev=prev, now=NOW, errchain={**chain, "stage": stage})
                assert (v.state, v.dominant) == (state, "storage"), (lineage, prev)
                assert v.action == _ACTIONS["storage"]


def test_key_trends_immature_ignores_zero_point_series() -> None:
    absent = {
        "smart_pending": _trend("insufficient", n_points=0),
        "smart_realloc": _trend("insufficient", n_points=0),
    }
    v = _call({"storage_risk": _st()}, trends=absent)
    assert IMMATURE not in v.missing_evidence


def test_systemic_counts_only_storage_thermal_os() -> None:
    soft = {
        "storage_risk": _st(20, damage=20),
        "disk_fill_risk": _axis(50),
        "network_risk": _axis(50),
        "software_aging_risk": _axis(50, coords={"flags": []}),
    }
    assert _call(soft).dominant != "systemic"
    hard = {"storage_risk": _st(20, damage=20), "os_degradation_risk": _axis(20)}
    v = _call(hard, trends={"throttle": _trend()})
    assert v.dominant == "systemic"
    assert v.state == "h2"


def test_trajectory_dominant_reattributed_to_disk_fill() -> None:
    axes = {"storage_risk": _st(), "trajectory_risk": _axis(60)}
    v = _call(axes, trends={"disk_fill": _trend(eta=70)})
    assert v.dominant == "disk_fill"
    assert v.action == FILL_ACTION
    # the driver is the worsening depletion trend with the smallest ETA
    spare = _call(axes, trends={"disk_fill": _trend(eta=70), "nvme_spare": _trend(eta=40)})
    assert spare.dominant == "storage"
    assert _call(axes).dominant == "trajectory"  # no driver -> stays trajectory


def test_wear_driven_trajectory_keeps_its_name() -> None:
    """D6, review R2: only the depletion trend that produced the trajectory value (C1: the
    riskiest domain) is its driver. Wear capped at the 180-day band (35) drives it here;
    a slow fill (ETA 613 days -> risk 5) must not turn it into «заполнение диска»."""
    axes = {"storage_risk": _st(), "trajectory_risk": _axis(35)}
    for trends in (
        {"storage_wear": _trend(eta=92), "disk_fill": _trend(eta=613)},
        {"storage_wear": _trend(eta=92), "nvme_spare": _trend(eta=400)},
    ):
        v = _call(axes, trends=trends)
        assert v.dominant == "trajectory", trends
        assert v.action == _ACTIONS["trajectory"]
    # a fill as risky as the wear (ETA 100 days -> 35) is a driver again
    tied = _call(axes, trends={"storage_wear": _trend(eta=92), "disk_fill": _trend(eta=100)})
    assert tied.dominant == "disk_fill"


def test_withheld_fill_eta_is_no_mechanism() -> None:
    """Review R2: a 24-72 h disk_fill trend is 'worsening' while the 72 h span gate still
    withholds its ETA (C1). A thresholded depletion trend is material only with an ETA
    within a year, so a good fill axis stays background: no dominant, the observe action.
    Counters (no boundary, never an ETA) keep the direction-only rule."""
    axes = {"storage_risk": _st(), "disk_fill_risk": _axis(5)}
    for key in ("disk_fill", "nvme_spare", "storage_wear"):
        v = _call(axes, trends={key: _trend(eta=None)})
        assert (v.state, v.dominant, v.action) == ("h0", None, OBSERVE), key
    near = _call(axes, trends={"disk_fill": _trend(eta=200)})
    assert (near.dominant, near.action) == ("disk_fill", FILL_ACTION)
    counter = _call({"storage_risk": _st()}, trends={"smart_pending": _trend(eta=None)})
    assert counter.dominant == "storage"


# --------------------------------------------------------------------------- #
# Actions and horizon (D5, D7)
# --------------------------------------------------------------------------- #
def test_h0_storage_dominant_gets_observe_action() -> None:
    v = _call({"storage_risk": _st(30)})  # band watch, zero coordinates
    assert (v.state, v.dominant) == ("h0", "storage")
    assert v.action == OBSERVE  # no colon: Zabbix joins "label: action"


def test_h0_bad_storage_axis_gets_check_action() -> None:
    v = _call({"storage_risk": _st(60)})  # band bad from legacy-only points
    assert v.state == "h0"
    assert v.action == CHECK


def test_h0_with_forecast_keeps_dominant_action() -> None:
    v = _call({"storage_risk": _st(30)}, trends={"nvme_spare": _trend(eta=150)})
    assert v.state == "h0"
    assert (v.horizon_days, v.horizon_kind) == (180, "eta")
    assert v.horizon_reason == (
        "прогноз по тренду (резерв NVMe до порога производителя): предел в пределах 180 дней"
    )
    assert v.action == _ACTIONS["storage"]


def test_h0_network_dominant_keeps_network_action() -> None:
    v = _call({"storage_risk": _st(), "network_risk": _axis(50)})
    assert (v.state, v.dominant) == ("h0", "network")
    assert v.action == _ACTIONS["network"]


def test_eta_shorter_than_state_window_wins_with_basis() -> None:
    axes = {"storage_risk": _st(60, damage=60, flags=["pending_gt10"])}
    v = _call(axes, trends={"disk_fill": _trend(eta=5)})
    assert v.state == "h3"
    assert (v.horizon_days, v.horizon_kind) == (7, "eta")
    assert "прогноз по тренду" in v.horizon_reason and "5%" in v.horizon_reason


def test_eta_zero_says_limit_reached() -> None:
    v = _call({"storage_risk": _st()}, trends={"disk_fill": _trend(eta=0.0)})
    assert (v.horizon_days, v.horizon_kind) == (7, "eta")
    assert v.horizon_reason == (
        "прогноз по тренду (свободное место на системном диске до 5%): "
        "предел уже достигнут — действовать в течение 7 дней"
    )


def test_eta_tie_with_state_window_goes_to_the_forecast() -> None:
    """D5 / §5.20: a forecast bucket equal to the state window wins with its basis."""
    fill = "прогноз по тренду (свободное место на системном диске до 5%): предел в пределах"
    cases = (
        (_st(60, damage=60, flags=["pending_gt10"]), 20, "h3", 30),  # eta in (7, 30]
        (_st(20, flags=["remap_masking"]), 60, "h2", 90),  # eta in (30, 90]: S9
    )
    for axis, eta, state, days in cases:
        v = _call({"storage_risk": axis}, trends={"disk_fill": _trend(eta=eta)})
        assert (v.state, v.horizon_days, v.horizon_kind) == (state, days, "eta"), state
        assert v.horizon_reason == f"{fill} {days} дней"


def test_storage_wear_eta_gives_no_horizon() -> None:
    v = _call({"storage_risk": _st()}, trends={"storage_wear": _trend(eta=20)})
    assert (v.horizon_days, v.horizon_kind) == (None, None)


def test_eta_beyond_180_days_gives_no_horizon() -> None:
    for key in ("disk_fill", "nvme_spare"):
        v = _call({"storage_risk": _st()}, trends={key: _trend(eta=181)})
        assert (v.horizon_days, v.horizon_reason, v.horizon_kind) == (None, "", None), key


# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #
def test_held_state_has_evidence_and_prev_dominant() -> None:
    prev = _prev("h3", dominant="storage", below_h=10.0, below_seen_at=_seen(4))
    v = _call({"storage_risk": _st()}, prev=prev, now=NOW)
    assert v.state == "h3"
    assert v.dominant == "storage"  # raw has no dominant -> the held verdict's one
    assert v.action == _ACTIONS["storage"]
    note = "состояние удержано: снижение подтверждается после 48 ч наблюдения без ухудшения"
    assert f"{note} (набрано 14 ч)" in _labels(v.state_evidence)
    assert f"{note} (набрано 14 ч)" in _labels(v.factors)


def _bayes_factor(level: Optional[str]) -> bool:
    classes = [] if level is None else [{"name": "network", "level": level}]
    bayes = {"classes": classes, "top": "network", "overall": 0.5}
    v = _call({"storage_risk": _st(50, damage=50)}, bayes=bayes)
    return any("bayes" in (f.get("label") or "") for f in v.factors)


def test_bayes_context_factor_only_when_top_level_not_low() -> None:
    assert _bayes_factor("high")
    assert not _bayes_factor("low")
    assert not _bayes_factor(None)  # level unknown for the top class -> no factor


def test_verdict_carries_new_keys() -> None:
    blob = dataclasses.asdict(_call({"storage_risk": _st(60, damage=60, flags=["accel"])}))
    keys = ("horizon_kind", "below_h", "below_seen_at", "worst_disk_id", "disk_ids")
    for key in (*keys, "attr_disk_ids", "had_disk"):
        assert key in blob, key
    assert blob["horizon_kind"] == "state"
    assert (blob["worst_disk_id"], blob["disk_ids"]) == ("hA", ["WD Blue", "hA"])
    assert blob["attr_disk_ids"] == ["hA"]
    assert blob["had_disk"] is True
