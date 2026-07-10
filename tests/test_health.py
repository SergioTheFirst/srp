"""ssd3 Ф6 -- tests for server.analytics.health (pure (D,R,O) assembly).

Every fixture is a plain dict shaped like the persisted ``risk_block`` pieces
(``score100`` axes, ``trajectory`` trends, ``errchain``); this module never
imports the producer dataclasses -- it consumes their serialised form.
"""

from __future__ import annotations

import dataclasses
import re
from datetime import datetime, timedelta
from typing import Any, Optional

import pytest
from server.analytics.health import (
    _ACTIONS,
    _BLIND_ACTION,
    HealthVerdict,
    compute_health,
    health_staleness,
)
from server.scoring.score100 import band_for_risk_score

IMMATURE = "оценка устойчивости незрелая"
NET_ACTION = "проверить линк/кабель/точку доступа"

_CYR = re.compile(r"[А-Яа-яЁё]")


def _has_cyr(text: str) -> bool:
    return bool(_CYR.search(text or ""))


# --------------------------------------------------------------------------- #
# dict fixture builders (shaped exactly like score_to_dict / trend_to_dict)
# --------------------------------------------------------------------------- #
def _axis(
    value: Optional[float],
    *,
    confidence: str = "high",
    factors: Optional[list] = None,
    coords: Optional[dict] = None,
    worst_disk: Optional[str] = None,
) -> dict:
    lineage: dict[str, Any] = {}
    if worst_disk is not None:
        lineage["worst_disk"] = worst_disk
    d: dict[str, Any] = {
        "value": value,
        "direction": "higher_is_worse",
        "band": band_for_risk_score(value),
        "confidence": confidence,
        "factors": list(factors or []),
        "missing_evidence": [],
        "source_lineage": lineage,
        "reason": "",
    }
    if coords is not None:
        d["coords"] = coords
    return d


def _st_coords(damage: float = 0.0, rloss: float = 0.0, flags: Optional[list] = None) -> dict:
    return {"damage": damage, "resilience_loss": rloss, "flags": list(flags or [])}


def _trend(
    *,
    direction: str = "worsening",
    n_points: int = 8,
    eta_days: Optional[int] = None,
    accelerating: bool = False,
    slope_per_day: float = 1.0,
    current: Optional[float] = None,
) -> dict:
    return {
        "metric": "m",
        "n_points": n_points,
        "current": current,
        "slope_per_day": slope_per_day,
        "eta_days": eta_days,
        "target_date": None,
        "direction": direction,
        "reason": "",
        "slope_recent": slope_per_day,
        "accelerating": accelerating,
    }


def _mature_key_trend() -> dict:
    # a mature but stable key trend so resilience isn't flagged immature
    return {"nvme_spare": _trend(direction="stable", n_points=8, slope_per_day=0.0)}


def _call(score100_axes, *, bayes=None, trends=None, errchain=None, cohort=None, prev=None):
    return compute_health(
        score100_axes,
        bayes or {"classes": [], "top": None, "overall": 0.0},
        trends or {},
        errchain,
        cohort,
        prev,
    )


# --------------------------------------------------------------------------- #
# Coordinate assembly
# --------------------------------------------------------------------------- #
def test_damage_is_max_of_channels() -> None:
    axes = {
        "storage_risk": _axis(20, coords=_st_coords(damage=20)),
        "battery_risk": _axis(30),
        "os_degradation_risk": _axis(50),  # *0.6 -> 30
    }
    v = _call(axes, trends=_mature_key_trend())
    assert v.damage.value == 30  # max(20, 30, 30)


def test_damage_os_multiplier_is_0_6() -> None:
    # os alone: 50 * 0.6 = 30 (partially reversible)
    axes = {"os_degradation_risk": _axis(50)}
    v = _call(axes, trends=_mature_key_trend())
    assert v.damage.value == 30


def test_rloss_max_plus_spare_and_tail_surcharges() -> None:
    axes = {"storage_risk": _axis(30, coords=_st_coords(rloss=10))}
    trends = {
        "nvme_spare": _trend(direction="worsening", n_points=8),  # +25
        "disk_tail_ratio": _trend(direction="worsening", n_points=8),  # +20
    }
    v = _call(axes, trends=trends)
    # rloss = 10 + 25 + 20 = 55 -> R = 45
    assert v.resilience.value == 45
    assert "spare_depleting" in v.resilience.flags
    assert "tail_ratio_worsening" in v.resilience.flags


def test_none_channels_excluded_from_max() -> None:
    axes = {
        "storage_risk": _axis(None),  # dead source -> excluded even if coords exist
        "battery_risk": _axis(40),
    }
    v = _call(axes, trends=_mature_key_trend())
    assert v.damage.value == 40


def test_zero_coords_from_dead_axis_excluded_k5() -> None:
    # storage axis value None but coords floaty-zero: must NOT count as "known 0".
    axes = {"storage_risk": _axis(None, coords=_st_coords(damage=0.0, rloss=0.0))}
    v = _call(axes, trends=_mature_key_trend())
    assert v.damage.value is None  # no known D channel


def test_all_none_channels_give_none_coordinate() -> None:
    axes = {"storage_risk": _axis(None), "battery_risk": _axis(None)}
    v = _call(axes, trends=_mature_key_trend())
    assert v.damage.value is None


# --------------------------------------------------------------------------- #
# K-pins
# --------------------------------------------------------------------------- #
def test_k1_same_coordinates_same_state_despite_raw_axis_swap() -> None:
    a = {"storage_risk": _axis(50, coords=_st_coords(damage=50))}
    # b reduces to the same D/R/O but via a different raw axis mix (extra low battery)
    b = {
        "storage_risk": _axis(50, coords=_st_coords(damage=50)),
        "battery_risk": _axis(30),  # < 50, does not change D
    }
    va = _call(a, trends=_mature_key_trend())
    vb = _call(b, trends=_mature_key_trend())
    assert va.damage.value == vb.damage.value == 50
    assert va.state == vb.state  # raw swap that holds the coordinate holds the state


def test_k1_changing_a_coordinate_changes_state() -> None:
    high = {"storage_risk": _axis(50, coords=_st_coords(damage=50))}
    low = {"storage_risk": _axis(10, coords=_st_coords(damage=10))}
    assert (
        _call(high, trends=_mature_key_trend()).state
        != _call(low, trends=_mature_key_trend()).state
    )


def test_k4_static_reading_no_dynamics() -> None:
    axes = {"storage_risk": _axis(50, coords=_st_coords(damage=50, flags=["damage_present"]))}
    v = _call(axes, trends={}, errchain=None)  # no series / trends / events
    assert v.resilience.flags == []  # static levels never invent R-dynamics
    assert v.confidence in ("medium", "low", "unknown")  # capped at medium
    assert IMMATURE in v.missing_evidence


def test_k5_blind_device_is_unknown_not_healthy() -> None:
    v = _call({}, trends={}, errchain=None)  # no SMART, no events, nothing
    assert v.observability.value < 40
    assert v.state == "unknown"
    assert v.state != "h0"
    assert v.band != "good"
    assert v.index is None
    assert v.blind_spots  # non-empty


def test_k2_pure_aging_no_damage_contribution() -> None:
    axes = {
        "storage_risk": _axis(0.0, coords=_st_coords(damage=0.0, rloss=0.0)),  # healthy disk
        "software_aging_risk": _axis(70, coords={"flags": ["aging_leak"]}),
    }
    v = _call(axes, trends=_mature_key_trend())
    assert v.damage.value == 0  # aging feeds R only, never D (K2)
    assert v.resilience.value == 30  # rloss 70
    assert "aging_leak" not in v.damage.flags


# --------------------------------------------------------------------------- #
# State ladder
# --------------------------------------------------------------------------- #
def test_state_h0_healthy() -> None:
    axes = {"storage_risk": _axis(0.0, coords=_st_coords())}
    assert _call(axes, trends=_mature_key_trend()).state == "h0"


def test_state_h1_early_damage() -> None:
    axes = {"storage_risk": _axis(20, coords=_st_coords(damage=20))}
    assert _call(axes, trends=_mature_key_trend()).state == "h1"


def test_state_h2_compensation() -> None:
    axes = {"storage_risk": _axis(30, coords=_st_coords(rloss=25, flags=["remap_masking"]))}
    assert _call(axes, trends=_mature_key_trend()).state == "h2"


def test_state_h3_accel() -> None:
    axes = {"storage_risk": _axis(50, coords=_st_coords(damage=40, flags=["accel"]))}
    v = _call(axes, trends=_mature_key_trend())
    assert v.state == "h3"
    assert v.horizon_days == 30


def test_state_h4_hard_flag() -> None:
    axes = {"storage_risk": _axis(70, coords=_st_coords(damage=70, flags=["predict_fail"]))}
    assert _call(axes, trends=_mature_key_trend()).state == "h4"


def test_compensation_pin_clean_smart_is_not_healthy() -> None:
    # avg latency normal, 197 fell while 5 rose (remap_masking) AND tail worsening
    axes = {
        "storage_risk": _axis(
            30,
            factors=[{"label": "диск маскирует дефекты переназначением", "delta": 25}],
            coords=_st_coords(damage=0.0, rloss=25, flags=["remap_masking"]),
        )
    }
    trends = {"disk_tail_ratio": _trend(direction="worsening", n_points=8)}
    v = _call(axes, trends=trends)
    assert v.state == "h2"
    assert v.band in ("watch", "bad")  # "clean SMART" is not healthy
    assert "remap_masking" in v.resilience.flags
    assert "tail_ratio_worsening" in v.resilience.flags


# --------------------------------------------------------------------------- #
# Ratchet (hysteresis)
# --------------------------------------------------------------------------- #
def test_ratchet_worsening_is_free() -> None:
    axes = {"storage_risk": _axis(70, coords=_st_coords(damage=70, flags=["predict_fail"]))}
    v = _call(axes, trends=_mature_key_trend(), prev={"state": "h0"})
    assert v.state == "h4"


def test_ratchet_h3_to_h1_forbidden_without_evidence() -> None:
    axes = {"storage_risk": _axis(20, coords=_st_coords(damage=20))}  # computes h1
    v = _call(
        axes,
        trends={"smart_pending": _trend(direction="worsening", n_points=8)},
        prev={"state": "h3"},
    )
    assert v.state == "h3"  # no positive evidence -> hold


def test_ratchet_flat_counters_one_step_up() -> None:
    axes = {"storage_risk": _axis(20, coords=_st_coords(damage=20))}  # computes h1
    flat = {
        "smart_pending": _trend(direction="stable", n_points=14, slope_per_day=0.0),
        "smart_realloc": _trend(direction="stable", n_points=14, slope_per_day=0.0),
        "smart_media_errors": _trend(direction="stable", n_points=14, slope_per_day=0.0),
    }
    v = _call(axes, trends=flat, prev={"state": "h3"})
    assert v.state == "h2"  # exactly one step of improvement, not h1


def test_ratchet_disk_replacement_permits_reset() -> None:
    axes = {"storage_risk": _axis(0.0, coords=_st_coords(), worst_disk="diskB")}
    v = _call(axes, trends=_mature_key_trend(), prev={"state": "h4", "worst_disk": "diskA"})
    assert v.state == "h0"  # new hardware -> full reset permitted


def test_ratchet_reboot_restores_clears_one_aging_rung() -> None:
    axes = {
        "storage_risk": _axis(0.0, coords=_st_coords()),
        "software_aging_risk": _axis(0.0, coords={"flags": ["reboot_restores"]}),
    }
    v = _call(axes, trends=_mature_key_trend(), prev={"state": "h2"})
    assert v.state == "h1"  # one step up on reboot evidence


# --------------------------------------------------------------------------- #
# Index
# --------------------------------------------------------------------------- #
def test_index_damage_100_is_low() -> None:
    axes = {"storage_risk": _axis(100, coords=_st_coords(damage=100))}
    v = _call(axes, trends=_mature_key_trend())
    assert v.index is not None and v.index <= 30


def test_index_rloss_100_is_low() -> None:
    axes = {"storage_risk": _axis(100, coords=_st_coords(rloss=100))}
    v = _call(axes, trends=_mature_key_trend())
    assert v.index is not None and v.index <= 30


def test_index_zero_loss_is_100() -> None:
    axes = {"storage_risk": _axis(0.0, coords=_st_coords(damage=0.0, rloss=0.0))}
    v = _call(axes, trends=_mature_key_trend())
    assert v.index == 100


def test_index_all_none_is_unknown() -> None:
    # everything clean except we force both coords None by giving no D/R channels
    axes = {"network_risk": _axis(80)}  # only context axis -> D None, Rloss None
    v = _call(axes, trends=_mature_key_trend())
    # O may or may not clear 40; either way both coords None -> index None
    assert v.index is None


def test_network_only_leaves_coordinates_clean() -> None:
    axes = {
        "storage_risk": _axis(0.0, coords=_st_coords()),  # clean, keeps O live
        "network_risk": _axis(80),
    }
    v = _call(axes, trends=_mature_key_trend())
    assert v.state == "h0"
    assert v.dominant == "network"
    assert v.action == NET_ACTION


# --------------------------------------------------------------------------- #
# Reconciliation / dominant / horizon / action
# --------------------------------------------------------------------------- #
def test_reconcile_h4_band_is_bad() -> None:
    axes = {"storage_risk": _axis(70, coords=_st_coords(damage=70, flags=["predict_fail"]))}
    assert _call(axes, trends=_mature_key_trend()).band == "bad"


def test_reconcile_h2_band_no_better_than_watch() -> None:
    # small damage but a compensation flag -> h2; index would be "good" without clamp
    axes = {
        "storage_risk": _axis(10, coords=_st_coords(damage=5, rloss=5, flags=["remap_masking"]))
    }
    v = _call(axes, trends=_mature_key_trend())
    assert v.state == "h2"
    assert v.band in ("watch", "bad")


def test_h0_with_bad_band_never_happens() -> None:
    # sweep several fixtures: no verdict may pair h0 with band bad (defensive clamp)
    fixtures = [
        {"storage_risk": _axis(0.0, coords=_st_coords())},
        {"storage_risk": _axis(10, coords=_st_coords(damage=10))},
        {"storage_risk": _axis(0.0, coords=_st_coords()), "network_risk": _axis(90)},
    ]
    for f in fixtures:
        v = _call(f, trends=_mature_key_trend())
        assert not (v.state == "h0" and v.band == "bad")


def test_systemic_three_watch_axes() -> None:
    axes = {
        "storage_risk": _axis(20, coords=_st_coords(damage=20)),
        "battery_risk": _axis(20),
        "os_degradation_risk": _axis(20),
    }
    v = _call(axes, trends=_mature_key_trend())
    assert v.dominant == "systemic"
    assert v.state in ("h2", "h3", "h4")  # floored at h2
    assert v.band in ("watch", "bad")  # reconciliation must clamp systemic-floored state too


def test_systemic_never_overrides_blind_zone() -> None:
    # >=3 watch/bad axes would normally floor state at h2, but K5 (blind device
    # can never read as healthier than "unknown") outranks the systemic floor.
    axes = {
        "battery_risk": _axis(20),
        "os_degradation_risk": _axis(20),
        "disk_fill_risk": _axis(20),
    }
    v = _call(axes, trends={}, errchain=None)
    assert v.observability.value is not None and v.observability.value < 40
    assert v.state == "unknown"  # not floored to h2
    assert v.action == _BLIND_ACTION


def test_horizon_state_beats_eta() -> None:
    axes = {"storage_risk": _axis(50, coords=_st_coords(damage=40, flags=["accel"]))}
    trends = {"nvme_spare": _trend(direction="worsening", n_points=8, eta_days=90)}
    v = _call(axes, trends=trends)
    assert v.state == "h3"
    assert v.horizon_days == 30  # state rule (30) beats the 90-day ETA


def test_horizon_nvme_spare_eta_under_30() -> None:
    axes = {"storage_risk": _axis(20, coords=_st_coords(damage=20))}  # h1 -> no state horizon
    trends = {"nvme_spare": _trend(direction="worsening", n_points=8, eta_days=20)}
    v = _call(axes, trends=trends)
    assert v.horizon_days == 30


def test_actions_cover_every_dominant_key() -> None:
    # the closed action map must cover exactly the 9 documented keys (8 + None)
    assert set(_ACTIONS) == {
        "storage",
        "aging",
        "os",
        "disk_fill",
        "battery",
        "network",
        "trajectory",
        "systemic",
        None,
    }
    # one clean storage keeps SMART live (O>=40) so state/dominant are real
    per_key = {
        "aging": {"software_aging_risk": _axis(50, coords={"flags": []})},
        "os": {"os_degradation_risk": _axis(50)},
        "disk_fill": {"disk_fill_risk": _axis(50)},
        "battery": {"battery_risk": _axis(50)},
        "network": {"network_risk": _axis(50)},
        "trajectory": {"trajectory_risk": _axis(50)},
    }
    for short, extra in per_key.items():
        axes = {"storage_risk": _axis(0.0, coords=_st_coords()), **extra}
        v = _call(axes, trends=_mature_key_trend())
        assert v.dominant == short
        assert v.action == _ACTIONS[short]  # each key routes to its own russian action
        assert _has_cyr(v.action)
    # storage dominant needs real storage damage to win max(r*s)
    storage_v = _call(
        {"storage_risk": _axis(50, coords=_st_coords(damage=50))}, trends=_mature_key_trend()
    )
    assert storage_v.dominant == "storage"
    assert storage_v.action == _ACTIONS["storage"]
    # None dominant (no risky axis at all)
    none_v = _call({"storage_risk": _axis(0.0, coords=_st_coords())}, trends=_mature_key_trend())
    assert none_v.dominant is None
    assert none_v.action == _ACTIONS[None]
    # systemic
    sysv = _call(
        {
            "storage_risk": _axis(20, coords=_st_coords(damage=20)),
            "battery_risk": _axis(20),
            "os_degradation_risk": _axis(20),
        },
        trends=_mature_key_trend(),
    )
    assert sysv.dominant == "systemic"
    assert sysv.action == _ACTIONS["systemic"]
    # blind (O<40) overrides all of the above
    blind_v = _call({}, trends={}, errchain=None)
    assert "восстановить видимость" in blind_v.action


def test_bayes_disagreement_is_a_factor_not_an_override() -> None:
    axes = {
        "storage_risk": _axis(50, coords=_st_coords(damage=50)),
        "network_risk": _axis(10),
    }
    v = _call(
        axes, bayes={"classes": [], "top": "network", "overall": 0.5}, trends=_mature_key_trend()
    )
    assert v.dominant == "storage"  # coordinate-derived wins
    assert any("bayes" in (f.get("label") or "") for f in v.factors)


# --------------------------------------------------------------------------- #
# Cohort (context only)
# --------------------------------------------------------------------------- #
def test_cohort_below_min_no_factor() -> None:
    axes = {"storage_risk": _axis(0.0, coords=_st_coords())}
    trends = {
        "nvme_spare": _trend(direction="stable", n_points=8, slope_per_day=0.0),
        "boot_time": _trend(direction="worsening", n_points=8, current=9000.0),
    }
    v = _call(axes, trends=trends, cohort={"cohort_size": 3, "boot_p90_ms": 5000.0})
    assert not any("90%" in (f.get("label") or "") for f in v.factors)


def test_cohort_above_p90_adds_context_factor_only() -> None:
    axes = {"storage_risk": _axis(0.0, coords=_st_coords())}
    trends = {
        "nvme_spare": _trend(direction="stable", n_points=8, slope_per_day=0.0),
        "boot_time": _trend(direction="worsening", n_points=8, current=9000.0),
    }
    cohort = {"cohort_size": 8, "boot_p90_ms": 5000.0}
    v = _call(axes, trends=trends, cohort=cohort)
    assert any("90%" in (f.get("label") or "") for f in v.factors)
    assert v.state == "h0"  # context never touches state


# --------------------------------------------------------------------------- #
# RU/EN split
# --------------------------------------------------------------------------- #
def test_ru_en_split() -> None:
    axes = {
        "storage_risk": _axis(
            60,
            factors=[{"label": "прошивка предсказывает отказ диска", "delta": 70}],
            coords=_st_coords(damage=60, flags=["predict_fail"]),
        )
    }
    v = _call(axes, trends=_mature_key_trend())
    # machine values -> ASCII/English
    assert v.state.isascii()
    assert v.band.isascii()
    assert v.confidence.isascii()
    assert (v.dominant or "storage").isascii()
    for flag in v.damage.flags + v.resilience.flags:
        assert flag.isascii()
    # operator-facing prose -> Cyrillic
    assert _has_cyr(v.state_label)
    assert _has_cyr(v.dominant_label)
    assert _has_cyr(v.action)
    if v.horizon_reason:
        assert _has_cyr(v.horizon_reason)
    for blind in v.blind_spots:
        assert _has_cyr(blind)
    for f in v.damage.evidence + v.resilience.evidence:
        assert _has_cyr(f.get("label") or "")


def test_returns_frozen_healthverdict() -> None:
    axes = {"storage_risk": _axis(0.0, coords=_st_coords())}
    v = _call(axes, trends=_mature_key_trend())
    assert isinstance(v, HealthVerdict)
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.state = "h4"  # type: ignore[misc]


def test_ratchet_immature_flat_trend_is_not_evidence() -> None:
    # a fresh-but-incidentally-flat trend (n_points < _MATURE_POINTS) must NOT
    # count as flat-counter evidence -- that would defeat the hysteresis gate.
    axes = {"storage_risk": _axis(20, coords=_st_coords(damage=20))}  # computes h1
    immature_flat = {"smart_pending": _trend(direction="stable", n_points=3, slope_per_day=0.0)}
    v = _call(axes, trends=immature_flat, prev={"state": "h3"})
    assert v.state == "h3"  # no legitimate evidence -> hold, not one step up


def test_blind_zone_empties_state_evidence_but_keeps_factors() -> None:
    # D/R evidence still reaches `factors` unconditionally; only `state_evidence`
    # (evidence for the firing *criterion*) is emptied, because in the blind
    # branch the firing criterion is O<40 itself, not a D/R criterion.
    axes = {
        "battery_risk": _axis(70, factors=[{"label": "износ батареи критический", "delta": 70}]),
    }
    v = _call(axes, trends={}, errchain=None)
    assert v.observability.value is not None and v.observability.value < 40
    assert v.state == "unknown"
    assert v.state_evidence == []
    assert any(f.get("label") == "износ батареи критический" for f in v.factors)


# --------------------------------------------------------------------------- #
# Staleness helper (read-side; NOT called by compute_health)
# --------------------------------------------------------------------------- #
def test_staleness_fresh_is_none() -> None:
    now = datetime(2026, 7, 10, 12, 0, 0)
    assert health_staleness((now - timedelta(days=2)).isoformat(), now) is None


def test_staleness_over_3_days() -> None:
    now = datetime(2026, 7, 10, 12, 0, 0)
    msg = health_staleness((now - timedelta(days=5)).isoformat(), now)
    assert msg is not None and _has_cyr(msg)
    assert "5" in msg


def test_staleness_over_10_days_signals_unknown() -> None:
    now = datetime(2026, 7, 10, 12, 0, 0)
    msg = health_staleness((now - timedelta(days=12)).isoformat(), now)
    assert msg is not None and _has_cyr(msg)
    # distinguishable from the 3-day message
    assert msg != health_staleness((now - timedelta(days=5)).isoformat(), now)


def test_staleness_unparseable_is_none() -> None:
    assert health_staleness("not-a-date", datetime(2026, 7, 10)) is None
