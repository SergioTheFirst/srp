"""Калибровка вердикта, D7: какую причину (доминанту) называет вердикт.

Вынесено из tests/test_calib_health.py (лимит 800 строк). Правила: доминанта = источник
ступени (H8, rung_source): флаг лестницы называет накопитель и в системном эпизоде,
«системная причина» -- у уровня, который поднял пол, и у системного цикла посреди
удержания; удержанный уровень показывает механизм, который его установил, и этот
механизм хранится отдельно от показанной метки (held_dominant), чтобы системная
метка посреди удержания не прилипала после конца системной картины.
"""

from __future__ import annotations

import dataclasses
import itertools
from datetime import timedelta

import pytest
from server.analytics.health import _ACTIONS, _BLIND_ACTION
from server.analytics.health_ladder import _rung, rung_source

from tests.test_calib_health import (
    CHECK,
    FILL_ACTION,
    GATED,
    LIVE_CHAIN,
    NOW,
    _axis,
    _call,
    _lineage,
    _no_storage,
    _prev,
    _st,
    _trend,
)

# Disk + heat from trends; the third counted mechanism is the OS axis in watch (OS_WATCH:
# 15 * 0.6 = 9 of damage, below h1). A boot trend is no longer one (H2, F-04).
SYSTEMIC = {
    "smart_realloc": _trend("worsening"),
    "throttle": _trend("worsening"),
}
OS_WATCH = _axis(15.0)


def test_chain_state_beats_any_other_dominant() -> None:
    """D7, review R2/R3: the disk chain drives h3/h4, so the verdict names the disk and its
    action whatever other axis is in watch/bad -- the OS axis the disk's BSOD lifted, a
    full system disk, a leak, the network -- never «переустановка ОС» or «освободить
    место» (a SMART-less disk: reliability counter only)."""
    crash = [{"label": "1 BSOD за 30 дней", "delta": 15.0}]
    lineage = _lineage(["nv", "NVMe SSD 256GB"], attr=[])
    cases = (
        ("chain_stage3", 25.0, "os_degradation_risk", {**_axis(27.0), "factors": crash}, "h4"),
        ("chain_stage2", 0.0, "os_degradation_risk", {**_axis(15.0), "factors": crash}, "h3"),
        ("chain_stage3", 25.0, "disk_fill_risk", _axis(30.0), "h4"),
        ("chain_stage2", 0.0, "disk_fill_risk", _axis(20.0), "h3"),
        ("chain_stage3", 25.0, "software_aging_risk", _axis(45.0, coords={"flags": []}), "h4"),
        ("chain_stage3", 25.0, "network_risk", _axis(50.0), "h4"),
    )
    for flag, st_value, name, other, state in cases:
        storage = _st(st_value, rloss=30, flags=[flag], lineage=lineage)
        stage = 3 if flag == "chain_stage3" else 2
        v = _call({"storage_risk": storage, name: other}, errchain={**LIVE_CHAIN, "stage": stage})
        assert (v.state, v.dominant) == (state, "storage"), (flag, name)
        assert v.action == _ACTIONS["storage"]


def test_a_disk_chain_names_the_disk_even_in_a_systemic_episode() -> None:
    """H8 (F-03): three mechanisms at once (storage + thermal + os), but the disk chain set
    the h4 -- it names the disk and its action; «системная причина» belongs only to a level
    the floor raised (the premise «a common cause explains the chain» set no rung)."""
    lineage = _lineage(["nv", "NVMe SSD 256GB"], attr=[])
    axes = {
        "storage_risk": _st(30.0, damage=30.0, flags=["chain_stage3"], lineage=lineage),
        "os_degradation_risk": _axis(30.0),
    }
    v = _call(axes, trends={"throttle": _trend("worsening")}, errchain={**LIVE_CHAIN, "stage": 3})
    assert (v.state, v.dominant, v.rung_source) == ("h4", "storage", "flag:chain_stage3")
    assert v.action == _ACTIONS["storage"]


def test_held_state_yields_to_a_systemic_cycle() -> None:
    """Review R3: the systemic floor keeps a releasing hold 'held' on every cycle, so a
    held state must not pin the old mechanism while three degradation mechanisms run --
    the common-cause action names what actually floors the state."""
    axes = {"storage_risk": _st(5.0), "os_degradation_risk": OS_WATCH}
    for prev in (_prev("h3"), _prev("h1", dominant="aging")):
        for cycle in range(30):  # 5 days at the 4 h cadence: past a 48 h release step
            now = NOW + timedelta(hours=4 * cycle)
            v = _call(axes, trends=SYSTEMIC, prev=prev, now=now)
            assert v.state in ("h2", "h3"), (prev["dominant"], cycle, v.state)
            assert (v.dominant, v.action) == ("systemic", _ACTIONS["systemic"]), cycle
            prev = dataclasses.asdict(v)


def test_storage_rung_beats_a_bigger_unrelated_axis() -> None:
    """Review R3: any disk flag that drives h3/h4 (D hard media or R growth) names the disk,
    not a coincidental higher axis such as a leaking app."""
    leak = _axis(75.0, coords={"flags": []})
    for flag in ("recurrence", "pending_gt10"):
        storage = _st(63.7, damage=45.0, rloss=30.0, flags=[flag])
        v = _call({"storage_risk": storage, "software_aging_risk": leak})
        assert (v.state, v.dominant) == ("h3", "storage"), flag
        assert v.action == _ACTIONS["storage"]


def test_held_state_keeps_the_mechanism_that_set_it() -> None:
    """D7: a frozen h4 of the disk (storage gated -> blind) must not borrow a coincidental
    current dominant: an OS axis in watch would tell the operator to reinstall Windows."""
    axes = {
        "storage_risk": _axis(None, lineage=GATED),
        "os_degradation_risk": _axis(30.0),
        "software_aging_risk": _axis(0.0, coords={"flags": []}),
    }
    v = _call(axes, prev=_prev("h4"), now=NOW, errchain=LIVE_CHAIN)
    assert (v.state, v.dominant) == ("h4", "storage")
    assert v.action == _ACTIONS["storage"]


def _gated_axes() -> dict:
    return {
        "storage_risk": _axis(None, lineage=GATED),
        "os_degradation_risk": _axis(30.0),
        "software_aging_risk": _axis(0.0, coords={"flags": []}),
    }


def _run(prev: dict, axes: dict, plan: list) -> list:
    """Round-trip the verdict through asdict every 4 h like the pipeline; plan = trends/cycle."""
    out = []
    for cycle, trends in enumerate(plan):
        now = NOW + timedelta(hours=4 * cycle)
        v = _call(axes, trends=trends, prev=prev, now=now, errchain=LIVE_CHAIN)
        out.append(v)
        prev = dataclasses.asdict(v)
    return out


def test_systemic_label_mid_hold_does_not_stick_on_a_frozen_hold() -> None:
    """Review R4 (U4): a frozen h4 of the disk shows «системная причина» only while the
    systemic picture lasts, then the disk again -- the label never becomes the hold's own
    mechanism."""
    plan = [SYSTEMIC] * 3 + [{}] * 12
    out = _run(_prev("h4"), _gated_axes(), plan)
    assert [v.dominant for v in out[:3]] == ["systemic"] * 3
    for v in out[3:]:
        assert (v.state, v.dominant, v.action) == ("h4", "storage", _ACTIONS["storage"])


def test_systemic_label_mid_hold_does_not_stick_on_a_releasing_hold() -> None:
    """Review R4 (U2): SMART visible, raw h1, the h4 releases step by step; after a short
    systemic episode the releasing levels are named by the disk again."""
    axes = {"storage_risk": _st(20.0, damage=20.0), "os_degradation_risk": OS_WATCH}
    plan = [SYSTEMIC] * 2 + [{}] * 14
    out = _run(_prev("h4"), axes, plan)
    assert [v.dominant for v in out[:2]] == ["systemic"] * 2  # the episode is real
    for v in out[2:]:
        assert v.dominant == "storage", (v.state, v.dominant)
        assert v.action == _ACTIONS["storage"]


def test_hold_without_mechanism_does_not_adopt_a_systemic_label() -> None:
    """A held level whose mechanism is unknown (None) keeps None as its mechanism: a
    systemic cycle mid-hold (not raising the level) must not become it."""
    prev = _prev("h3", dominant=None, held_dominant=None)  # above the h2 floor: held
    axes = {"storage_risk": _st(5.0), "os_degradation_risk": OS_WATCH}
    out = _run(prev, axes, [SYSTEMIC] + [{}] * 6)
    assert out[0].dominant == "systemic"
    assert all(v.dominant != "systemic" for v in out[1:]), [v.dominant for v in out]


def test_disk_flag_level_set_under_a_systemic_label_is_the_disk() -> None:
    """A level set by a live disk flag during a systemic episode names the disk at once
    (H8: the flag is the rung's source) and keeps it as its mechanism once the flag lapses
    and the level is held."""
    flagged = {
        "storage_risk": _st(40.0, damage=40.0, rloss=30.0, flags=["recurrence"]),
        "os_degradation_risk": OS_WATCH,
    }
    lapsed = {"storage_risk": _st(5.0)}
    prev = None
    v = _call(flagged, trends=SYSTEMIC, now=NOW, errchain=LIVE_CHAIN)
    assert (v.state, v.dominant, v.held_dominant) == ("h3", "storage", "storage")
    prev = dataclasses.asdict(v)
    held = _call(lapsed, prev=prev, now=NOW + timedelta(hours=4), errchain=LIVE_CHAIN)
    assert (held.state, held.dominant) == ("h3", "storage")


def test_floor_raising_a_held_level_keeps_the_hold_mechanism() -> None:
    """Review R5: the systemic floor lifting an h1 hold to h2 is not a fresh level -- the
    hold keeps its mechanism, so after the episode the disk is named again (releasing and
    frozen holds). The systemic cycles themselves still show 'systemic'."""
    releasing = {"storage_risk": _st(5.0), "os_degradation_risk": OS_WATCH}
    frozen = {**_gated_axes(), "os_degradation_risk": OS_WATCH}  # raw h0: a real hold
    for axes in (releasing, frozen):
        prev = _prev("h1", below_h=0.0, below_seen_at=NOW.isoformat())
        out = _run(prev, axes, [SYSTEMIC] * 2 + [{}] * 24)
        assert [v.dominant for v in out[:2]] == ["systemic"] * 2
        stale = [
            (k, v.state, v.dominant) for k, v in enumerate(out[2:]) if v.dominant == "systemic"
        ]
        assert not stale, stale
        held = [v.dominant for v in out[2:] if v.state != "h0"]
        assert held and set(held) == {"storage"}, held  # the disk is named again


def test_floor_raising_a_free_level_belongs_to_systemic() -> None:
    """A level the floor raises from a free (unheld) state was set by the three mechanisms:
    while that h2 is held after the episode it still names 'systemic'."""
    axes = {"storage_risk": _st(5.0), "os_degradation_risk": OS_WATCH}
    out = _run(None, axes, [SYSTEMIC] + [{}] * 3)
    first = (out[0].state, out[0].dominant, out[0].rung_source)
    assert first == ("h2", "systemic", "systemic_floor")
    assert all((v.state, v.dominant) == ("h2", "systemic") for v in out[1:])
    assert {v.rung_source for v in out[1:]} == {"hold"}


# --------------------------------------------------------------------------- #
# H8 (F-03): the dominant and the action follow the source of the rung
# --------------------------------------------------------------------------- #
LEAK = _axis(45.0, coords={"flags": []})


@pytest.mark.parametrize(
    ("axes", "source", "action"),
    [
        # two disk 153 retries: was h2 «данных недостаточно -- проверить агент»
        ({"storage_risk": _st(10.0, flags=["early_events"])}, "flag:early_events", CHECK),
        # remap masking beside a leaking app: was h2 «перезагрузить»
        (
            {"storage_risk": _st(10.0, flags=["remap_masking"]), "software_aging_risk": LEAK},
            "flag:remap_masking",
            CHECK,
        ),
        # heavy stable damage (D 45) beside the same leak: the disk and its own action
        (
            {
                "storage_risk": _st(30.0, damage=45.0, flags=["damage_present"]),
                "software_aging_risk": LEAK,
            },
            "heavy_storage",
            _ACTIONS["storage"],
        ),
        # the disk's own R loss beside an OS axis in watch: was «переустановка ОС»
        (
            {"storage_risk": _st(30.0, rloss=45.0), "os_degradation_risk": _axis(40.0)},
            "rloss:storage",
            _ACTIONS["storage"],
        ),
    ],
)
def test_a_storage_h2_names_the_disk(axes: dict, source: str, action: str) -> None:
    v = _call(axes)
    assert (v.state, v.dominant, v.rung_source) == ("h2", "storage", source)
    assert v.action == action


def test_a_trajectory_h2_is_not_named_by_a_bigger_network_axis() -> None:
    axes = {
        "storage_risk": _st(5.0),
        "trajectory_risk": _axis(60.0),  # R loss 51: h2
        "network_risk": _axis(90.0),  # 90 * 0.6 = 54 outranked the trajectory's 51
    }
    v = _call(axes)
    assert (v.state, v.dominant, v.rung_source) == ("h2", "trajectory", "rloss:trajectory")
    assert v.action == _ACTIONS["trajectory"]


def test_an_os_damage_h1_names_the_os_not_a_bigger_leak() -> None:
    axes = {
        "storage_risk": _st(5.0),
        "os_degradation_risk": _axis(30.0),  # D 18: h1
        "software_aging_risk": _axis(39.0, coords={"flags": []}),
    }
    v = _call(axes)
    assert (v.state, v.dominant, v.rung_source) == ("h1", "os", "d:os")
    assert v.action == _ACTIONS["os"]


def test_h0_with_a_fill_forecast_asks_to_free_space_not_to_replace_the_disk() -> None:
    # F-03: h0 + a 180-day fill forecast + a warm disk in watch -> was «снять образ данных»
    v = _call({"storage_risk": _st(30.0)}, trends={"disk_fill": _trend(eta=150)})
    assert (v.state, v.horizon_days, v.horizon_kind, v.rung_source) == ("h0", 180, "eta", None)
    assert (v.dominant, v.action) == ("disk_fill", FILL_ACTION)


def test_a_held_check_level_keeps_the_check_action() -> None:
    """The flag lapses (raw h0): the held h2 and its h1 step must not escalate to
    «снять образ» -- the held level keeps the source that set it (held_source)."""
    fresh = _call({"storage_risk": _st(10.0, flags=["remap_masking"])}, now=NOW)
    assert (fresh.action, fresh.held_source) == (CHECK, "flag:remap_masking")
    prev, held = dataclasses.asdict(fresh), []
    for cycle in range(1, 20):  # 76 h at 4 h: h2 held, then the 48 h step down to h1
        v = _call({"storage_risk": _st(10.0)}, prev=prev, now=NOW + timedelta(hours=4 * cycle))
        held += [v] if v.state != "h0" else []
        prev = dataclasses.asdict(v)
    assert {v.state for v in held} == {"h1", "h2"}
    assert {(v.dominant, v.action, v.rung_source) for v in held} == {("storage", CHECK, "hold")}


def test_confirmed_damage_ends_the_check_action_of_a_held_level() -> None:
    fresh = _call({"storage_risk": _st(10.0, flags=["remap_masking"])}, now=NOW)
    damaged = _st(10.0, damage=20.0, flags=["damage_present"])  # raw h1: below the held h2
    v = _call({"storage_risk": damaged}, prev=dataclasses.asdict(fresh), now=NOW)
    assert (v.state, v.dominant, v.action) == ("h2", "storage", _ACTIONS["storage"])


@pytest.mark.parametrize(
    ("d_parts", "r_parts", "flags", "source"),
    [
        ({}, {}, {"chain_stage3", "pending_gt10"}, "flag:chain_stage3"),
        ({}, {}, {"pending_gt10", "uncorrectable_198"}, "flag:pending_gt10"),  # two media: h4
        ({}, {"storage": 70.0}, {"recurrence"}, "flag:recurrence"),
        ({}, {"aging": 39.0, "trajectory": 70.0, "storage": 70.0}, set(), "rloss:storage"),
        ({}, {"storage": 45.0, "trajectory": 55.0}, set(), "rloss:storage"),  # same rung
        ({}, {"trajectory": 45.0}, {"fs_corruption"}, "rloss:trajectory"),  # H6: not alone
        ({}, {"storage": 45.0}, {"early_events"}, "rloss:storage"),  # the disk's own R loss
        ({"storage": 45.0}, {"trajectory": 51.0}, {"damage_present"}, "heavy_storage"),
        ({"storage": 45.0}, {}, {"damage_present", "remap_masking"}, "heavy_storage"),
        ({}, {"trajectory": 45.0}, {"early_events"}, "flag:early_events"),  # storage > R
        ({"storage": 45.0}, {}, {"damage_present", "fs_corruption"}, "heavy_storage"),
        ({}, {}, {"fs_corruption"}, "flag:fs_corruption"),
        ({"storage": 20.0, "os": 20.0}, {}, set(), "d:storage"),
        ({"os": 18.0}, {}, {"aging_leak"}, "d:os"),
        ({"os": 18.0}, {}, {"damage_present"}, "flag:damage_present"),  # storage > os
        ({}, {}, {"aging_leak"}, "flag:aging_leak"),
        ({}, {}, set(), None),
    ],
)
def test_rung_source_names_the_rule_that_set_the_rung(
    d_parts: dict, r_parts: dict, flags: set, source: str
) -> None:
    st_damage = d_parts.get("storage", 0.0)
    rv = 100.0 - max(r_parts.values(), default=0.0)
    state = _rung(max(d_parts.values(), default=0.0), rv, flags, st_damage)
    assert rung_source(state, d_parts, r_parts, flags, st_damage) == source


_SOFT_H2 = ("remap_masking", "early_events", "spare_depleting")  # storage H2 flags
_GRID_FLAGS = (
    "chain_stage3",
    "pending_gt10",
    "uncorrectable_198",
    "recurrence",
    "spare_consumed",
    *_SOFT_H2,
    "fs_corruption",
    "damage_present",
    "aging_leak",
)
_STORAGE_SOURCES = {"rloss:storage", "heavy_storage", *(f"flag:{f}" for f in _SOFT_H2)}


def test_every_level_above_h0_has_a_source_that_fired() -> None:
    """Spec C4 (grid): every h1+ names a rule, the named evidence is really there, and
    at a tie storage evidence wins: the disk's own R loss or heavy damage before a soft
    flag (CHECK), any storage evidence before the trajectory, D storage before D os."""
    floors = {"h4": 0.0, "h3": 70.0, "h2": 40.0, "h1": 15.0}
    for bits in itertools.product((0, 1), repeat=len(_GRID_FLAGS)):
        flags = {f for f, on in zip(_GRID_FLAGS, bits) if on}
        for ds, do, rs, rt, ra in itertools.product(
            (0.0, 20.0, 45.0), (0.0, 18.0), (0.0, 45.0, 75.0), (0.0, 45.0, 75.0), (0.0, 39.0)
        ):
            d_parts = {"storage": ds, "os": do}
            r_parts = {"storage": rs, "trajectory": rt, "aging": ra}
            state = _rung(max(ds, do), 100.0 - max(rs, rt, ra), flags, ds)
            src = rung_source(state, d_parts, r_parts, flags, ds)
            if state == "h0":
                assert src is None
                continue
            kind, _, name = (src or "none:").partition(":")
            assert kind in ("flag", "rloss", "d", "heavy_storage"), (state, flags, src)
            if kind == "flag":
                assert name in flags
            elif kind in ("rloss", "d"):
                assert (r_parts if kind == "rloss" else d_parts)[name] >= floors[state]
            else:
                assert ds >= 40.0 and "damage_present" in flags
            heavy = ds >= 40.0 and "damage_present" in flags
            if state == "h2" and (rs >= 40.0 or heavy or flags & set(_SOFT_H2)):
                assert src in _STORAGE_SOURCES, (flags, rs, rt, ds, src)
            if state == "h2" and kind == "flag" and name in _SOFT_H2:
                assert rs < 40.0 and not heavy, (flags, rs, ds, src)
            if state == "h1" and (ds >= 15.0 or "damage_present" in flags):
                assert src in ("d:storage", "flag:damage_present"), (flags, ds, do, src)


# --------------------------------------------------------------------------- #
# Review 3h
# --------------------------------------------------------------------------- #
DAMAGED = _st(30.0, damage=30.0, flags=["damage_present"])  # raw h1


def _cycles(plan: list, *, now_step: float = 4.0) -> list:
    """Run storage axes cycle by cycle through asdict (live events keep O >= 40)."""
    out, prev = [], None
    for k, axes in enumerate(plan):
        v = _call(axes, prev=prev, now=NOW + timedelta(hours=now_step * k), errchain=LIVE_CHAIN)
        out.append(v)
        prev = dataclasses.asdict(v)
    return out


@pytest.mark.parametrize(
    "later",
    [{"storage_risk": _st(10.0)}, _no_storage(GATED)],  # 197 back to 0 / a blind cycle
    ids=["toggled-off", "gated"],
)
def test_damage_seen_in_a_check_episode_keeps_the_storage_action(later: dict) -> None:
    """Once damage_present is seen, the held level never claims «без подтверждённых
    повреждений» again: not on a blind cycle, not when 197 toggles back to 0."""
    masked = {"storage_risk": _st(30.0, damage=30.0, flags=["damage_present", "remap_masking"])}
    out = _cycles([masked, later, later])
    assert [v.state for v in out] == ["h2"] * 3
    assert {v.action for v in out} == {_ACTIONS["storage"]}
    checked = _cycles(
        [{"storage_risk": _st(10.0, flags=["early_events"])}, {"storage_risk": DAMAGED}, later]
    )
    assert [v.action for v in checked] == [CHECK, _ACTIONS["storage"], _ACTIONS["storage"]]


def test_without_damage_a_blind_cycle_keeps_the_check_action() -> None:
    out = _cycles([{"storage_risk": _st(10.0, flags=["early_events"])}, _no_storage(GATED)])
    assert [(v.state, v.action) for v in out] == [("h2", CHECK)] * 2


def test_a_live_disk_flag_names_the_disk_in_a_held_level_mid_episode() -> None:
    dying = _st(40.0, damage=40.0, rloss=30.0, flags=["chain_stage3", "pending_gt10"])
    first = _call({"storage_risk": dying}, now=NOW, errchain={**LIVE_CHAIN, "stage": 3})
    axes = {
        "storage_risk": _st(40.0, damage=40.0, rloss=30.0, flags=["pending_gt10"]),  # raw h3
        "os_degradation_risk": OS_WATCH,
    }
    v = _call(
        axes,
        trends=SYSTEMIC,
        prev=dataclasses.asdict(first),
        now=NOW + timedelta(hours=4),
        errchain=LIVE_CHAIN,
    )
    assert (v.state, v.rung_source, v.dominant) == ("h4", "hold", "storage")
    assert v.action == _ACTIONS["storage"]


def test_more_evidence_on_an_r_loss_h2_never_softens_the_action() -> None:
    tail = {"disk_tail_ratio": _trend()}
    base = _call({"storage_risk": _st(30.0, rloss=30.0)}, trends=tail)
    more = _call({"storage_risk": _st(30.0, rloss=45.0, flags=["early_events"])}, trends=tail)
    assert more.resilience.value < base.resilience.value
    for v in (base, more):
        assert (v.state, v.rung_source, v.action) == ("h2", "rloss:storage", _ACTIONS["storage"])


@pytest.mark.parametrize("trends", [{"disk_fill": _trend(eta=60)}, {}], ids=["fill", "none"])
def test_heavy_disk_damage_is_not_renamed_by_a_trajectory(trends: dict) -> None:
    axes = {
        "storage_risk": _st(70.0, damage=45.0, flags=["damage_present"]),
        "trajectory_risk": _axis(60.0),  # R loss 51: h2 on its own
    }
    v = _call(axes, trends=trends)
    assert (v.state, v.rung_source, v.dominant) == ("h2", "heavy_storage", "storage")
    assert v.action == _ACTIONS["storage"]


def test_a_nearer_spare_forecast_never_gets_a_milder_action() -> None:
    # spare_depleting is the forecast itself (ETA <= 90 d): the storage action, as at h0/h3
    for eta, traj, state in ((150, 35.0, "h0"), (60, 60.0, "h2"), (20, 90.0, "h3")):
        axes = {"storage_risk": _st(10.0), "trajectory_risk": _axis(traj)}
        v = _call(axes, trends={"nvme_spare": _trend(eta=eta)})
        assert (v.state, v.action) == (state, _ACTIONS["storage"]), eta


def test_unknown_with_a_fill_forecast_names_the_fill_but_asks_to_restore_visibility() -> None:
    axes = {
        "storage_risk": _axis(None, lineage=GATED),
        "software_aging_risk": _axis(0.0, coords={"flags": []}),
    }
    v = _call(axes, trends={"disk_fill": _trend(eta=20)}, now=NOW, errchain=LIVE_CHAIN)
    assert (v.state, v.horizon_days, v.horizon_kind, v.rung_source) == ("unknown", 30, "eta", None)
    assert (v.dominant, v.action) == ("disk_fill", _BLIND_ACTION)


@pytest.mark.parametrize(
    "first",
    [
        _st(30.0, damage=30.0, rloss=15.0, flags=["damage_present", "early_events"]),
        _st(
            45.0,
            damage=30.0,
            rloss=45.0,
            flags=["damage_present", "compensation_breach", "early_events"],
        ),  # what compute_storage_risk emits for 197=1 + two 153: the level is rloss:storage
    ],
    ids=["check-source", "rloss-storage"],
)
@pytest.mark.parametrize(
    "later",
    [
        _st(15.0, rloss=15.0, flags=["early_events"], lineage=_lineage(["hA"], attr=[])),
        _st(15.0, rloss=15.0, flags=["early_events"]),
    ],
    ids=["smart-blind", "toggled-off"],
)
def test_damage_seen_keeps_the_storage_action_while_retries_keep_the_level(first, later) -> None:
    """The retries keep the h2, so no hold engages: the next SMART-blind cycle or a 197
    toggle right after damage was seen never claims «без подтверждённых повреждений»."""
    out = _cycles([{"storage_risk": first}, {"storage_risk": later}, {"storage_risk": later}])
    assert [v.state for v in out] == ["h2"] * 3
    assert {v.action for v in out} == {_ACTIONS["storage"]}


def test_a_replaced_disk_does_not_inherit_the_damage_seen() -> None:
    first = _st(30.0, damage=30.0, rloss=15.0, flags=["damage_present", "early_events"])
    new = _st(15.0, rloss=15.0, flags=["early_events"], lineage=_lineage(["hB"], inventory=["hB"]))
    out = _cycles([{"storage_risk": first}, {"storage_risk": new}])
    assert (out[1].disk_replaced, out[1].action) == (True, CHECK)
