"""Spec H1 (2026-10-30): why a cycle is blind, and what the release clock does.

offline (gate + STALE: no fresh historical) and the first unknown episode pause the
clock; a real SMART loss and a repeated unknown episode reset it; a K5 unknown that is
not blind keeps the observed hours.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from server import pipeline
from server.analytics.health import compute_health
from server.analytics.health_hold import (
    RELEASE_HOURS,
    DiskView,
    Hold,
    blind_cause,
    disk_view,
    hold_notes,
    hold_state,
)
from server.analytics.storage import compute_storage_risk

_T0 = datetime(2026, 10, 1, 7, 0, tzinfo=timezone.utc)
_PREV_DISK = {"worst_disk_id": "d1", "disk_ids": ["d1"], "attr_disk_ids": ["d1"], "had_disk": True}


def _view(
    *,
    present: bool = True,
    gated: bool = False,
    ids: tuple = ("d1",),
    attrs: tuple = ("d1",),
    inventory: tuple = ("d1",),
    state: Optional[str] = None,
    collector: Optional[str] = None,
    ran: bool = False,
) -> DiskView:
    return DiskView(
        present=present,
        gated=gated,
        ids=frozenset(ids) if present and not gated else frozenset(),
        attr_ids=frozenset(attrs) if present and not gated else frozenset(),
        worst_id="d1",
        inventory=frozenset(inventory),
        source_state=state,
        source_collector=collector,
        source_ran=ran,
    )


def _held(below_h: Optional[float], **extra: object) -> dict:
    return {"state": "h3", "below_h": below_h, "below_seen_at": _T0.isoformat(), **extra}


# --------------------------------------------------------------------------- #
# blind_cause
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("view", "cause"),
    [
        (_view(), None),  # disks seen: not blind
        (_view(gated=True, state="stale"), "offline"),  # no fresh historical
        (_view(gated=True, state="suspect"), "unknown"),
        (_view(gated=True, state="unavailable", collector="timeout"), "unknown"),
        (_view(gated=True), "unknown"),  # no trust block: no hint
        (_view(ids=("d2",)), "loss"),  # worst disk dropped from SMART, still in inventory
        (_view(attrs=()), "loss"),  # a listed disk lost its SMART attributes
        (_view(present=False, collector="empty", ran=True), "loss"),  # script ran, no disks
        (_view(present=False, collector="empty"), "unknown"),  # PowerShell printed nothing
        (_view(present=False, collector="timeout"), "unknown"),
        (_view(present=False), "unknown"),
    ],
)
def test_blind_cause_classifies_the_rule_that_made_the_cycle_blind(
    view: DiskView, cause: Optional[str]
) -> None:
    assert blind_cause(view, _PREV_DISK) == cause


def test_blind_cause_without_a_previous_verdict_is_none() -> None:
    assert blind_cause(_view(gated=True, state="stale"), None) is None


def test_disk_view_reads_the_storage_source_from_the_lineage() -> None:
    axis = {
        "value": None,
        "source_lineage": {
            "storage_domain": "unknown",
            "storage_source": {"state": "stale", "collector_status": "ok", "historical_ran": True},
        },
    }
    view = disk_view(axis)
    assert (view.source_state, view.source_collector, view.source_ran) == ("stale", "ok", True)
    assert disk_view({"value": 1.0, "source_lineage": {}}).source_state is None


# --------------------------------------------------------------------------- #
# hold_state: pause vs reset
# --------------------------------------------------------------------------- #
def _hold(
    raw: str, prev: dict, cause: Optional[str], *, blind: bool = True, hours: float = 4
) -> Hold:
    return hold_state(
        raw, prev, now=_T0 + timedelta(hours=hours), blind=blind, replaced=False, cause=cause
    )


def _as_prev(h: Hold, cause: Optional[str], hours: float) -> dict:
    return {
        "state": h.state,
        "below_h": h.below_h,
        "below_seen_at": h.below_seen_at,
        "blind_cause": cause,
        "unknown_pause_used": h.pause_used,
        "seen": hours,
    }


@pytest.mark.parametrize("raw", ["h0", "unknown"])  # D8: a held h3 stays shown on a blind cycle
def test_offline_cycle_pauses_the_release_clock(raw: str) -> None:
    paused = _hold(raw, _held(30.0), "offline")
    assert (paused.state, paused.held, paused.below_h, paused.below_seen_at) == (
        "h3",
        True,
        30.0,
        None,
    )
    # the next visible cycle adds 0 h (nothing observed since), then time runs again
    back = hold_state(
        "h0",
        _as_prev(paused, "offline", 4),
        now=_T0 + timedelta(hours=5),
        blind=False,
        replaced=False,
    )
    assert back.below_h == 30.0
    later = hold_state(
        "h0",
        {**_as_prev(back, None, 5)},
        now=_T0 + timedelta(hours=9),
        blind=False,
        replaced=False,
    )
    assert later.below_h == 34.0


def test_loss_resets_the_clock_as_before() -> None:
    h = _hold("h0", _held(30.0), "loss")
    assert (h.state, h.held, h.below_h) == ("h3", True, None)


def test_blind_without_a_cause_keeps_the_old_reset() -> None:
    # callers that do not pass cause= (the guard in test_health.py) see the old behaviour
    h = hold_state("h0", _held(30.0), now=_T0, blind=True, replaced=False)
    assert (h.state, h.held, h.below_h) == ("h3", True, None)


def test_unknown_first_episode_pauses_and_spends_the_pause() -> None:
    first = _hold("h0", _held(30.0), "unknown")
    assert (first.below_h, first.pause_used, first.restarted) == (30.0, True, False)
    # the same episode goes on: still a pause
    again = _hold("h0", _as_prev(first, "unknown", 4), "unknown", hours=8)
    assert (again.below_h, again.pause_used, again.restarted) == (30.0, True, False)


def test_unknown_new_episode_after_the_pause_was_spent_resets_with_a_note() -> None:
    first = _hold("h0", _held(30.0), "unknown")
    seen = hold_state(
        "h0",
        _as_prev(first, "unknown", 4),
        now=_T0 + timedelta(hours=8),
        blind=False,
        replaced=False,
    )
    assert seen.pause_used and seen.below_h == 30.0  # the spent pause survives seen cycles
    second = _hold("h0", _as_prev(seen, None, 8), "unknown", hours=12)
    assert (second.below_h, second.restarted) == (None, True)
    notes = [n["label"] for n in hold_notes(second, True, False, cause="unknown")]
    assert any("начат заново" in n for n in notes)


def test_the_spent_pause_clears_on_a_step_down_and_when_free() -> None:
    prev = _held(RELEASE_HOURS - 1, unknown_pause_used=True)
    step = hold_state("h0", prev, now=_T0 + timedelta(hours=2), blind=False, replaced=False)
    assert step.state == "h2" and step.pause_used is False
    free = hold_state(
        "h3", _held(10.0, unknown_pause_used=True), now=_T0, blind=False, replaced=False
    )
    assert free.held is False and free.pause_used is False


def test_k5_unknown_without_blindness_keeps_the_observed_hours() -> None:
    k5 = hold_state("unknown", _held(20.0), now=_T0, blind=False, replaced=False)
    assert (k5.state, k5.held, k5.below_h, k5.below_seen_at) == ("unknown", False, 20.0, None)
    nxt = hold_state(
        "h0",
        {
            "state": "unknown",
            "last_known_state": "h3",
            "below_h": k5.below_h,
            "below_seen_at": k5.below_seen_at,
        },
        now=_T0 + timedelta(hours=4),
        blind=False,
        replaced=False,
    )
    assert (nxt.state, nxt.below_h) == ("h3", 20.0)


def test_pause_notes_name_the_cause() -> None:
    offline = hold_notes(_hold("h0", _held(30.0), "offline"), True, False, cause="offline")
    unknown = hold_notes(_hold("h0", _held(30.0), "unknown"), True, False, cause="unknown")
    loss = hold_notes(_hold("h0", _held(30.0), "loss"), True, False, cause="loss")
    assert "на паузе" in offline[0]["label"] and "свежих данных" in offline[0]["label"]
    assert "причина не установлена" in unknown[0]["label"]
    assert loss[0]["label"].startswith("SMART временно недоступен")


# --------------------------------------------------------------------------- #
# Office calendar: blind (offline) mornings never stall the release (F-01, G4)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("blind_every", [0, 3, 2, 1])  # P(blind morning) = 0, 1/3, 1/2, 1
def test_office_pc_releases_h3_within_21_days_despite_offline_mornings(blind_every: int) -> None:
    prev: dict = {"state": "h3", "below_h": None, "below_seen_at": None}
    released_at: Optional[int] = None
    for day in range(30):
        base = _T0 + timedelta(days=day)
        cycles = (
            [(base - timedelta(hours=1), True)] if blind_every and day % blind_every == 0 else []
        )
        cycles += [(base + timedelta(hours=4 * k), False) for k in range(4)]  # 07..19 h
        for now, blind in cycles:
            cause = "offline" if blind else None
            h = hold_state("h0", prev, now=now, blind=blind, replaced=False, cause=cause)
            prev = {
                "state": h.state,
                "below_h": h.below_h,
                "below_seen_at": h.below_seen_at,
                "blind_cause": cause,
                "unknown_pause_used": h.pause_used,
            }
        if prev["state"] == "h0" and released_at is None:
            released_at = day
    assert released_at is not None and released_at <= 21


def test_unknown_right_after_offline_is_the_same_episode() -> None:
    # D24 (replay 2026-10-30): the morning heartbeat sees STALE (offline), the historical a
    # minute later fails (unknown) -- one blind stretch, not a new episode: no reset
    prev = _held(36.0, unknown_pause_used=True)
    offline = _hold("h0", prev, "offline")
    same = _hold("h0", _as_prev(offline, "offline", 4), "unknown", hours=4.05)
    assert (same.below_h, same.restarted) == (36.0, False)


@pytest.mark.parametrize(
    ("raw", "blind", "cause"),
    [("h0", True, "offline"), ("h0", True, "loss"), ("unknown", False, None)],
)
def test_the_spent_pause_survives_offline_loss_and_k5_cycles(
    raw: str, blind: bool, cause: Optional[str]
) -> None:
    mid = _hold(raw, _held(30.0, unknown_pause_used=True), cause, blind=blind)
    assert mid.pause_used is True  # only a step-down or a free state clears it (H1)
    # so a new unknown episode after a seen cycle still resets, with the note
    back = {**_as_prev(mid, cause, 4), "last_known_state": "h3"}  # K5 shows unknown
    seen = _hold("h0", back, None, blind=False, hours=8)
    again = _hold("h0", _as_prev(seen, None, 8), "unknown", hours=12)
    assert (again.below_h, again.restarted) == (None, True)
    notes = hold_notes(again, True, False, cause="unknown")
    assert any("начат заново" in n["label"] for n in notes)


# --------------------------------------------------------------------------- #
# pipeline: the storage_source hint (spec H1 input)
# --------------------------------------------------------------------------- #
def _trust(**status: str) -> dict:
    sources = {"storage_reliability": {"state": "unavailable", "collector_status": "empty"}}
    sources.update({k: {"collector_status": v} for k, v in status.items()})
    return {"sources": sources}


@pytest.mark.parametrize(
    ("status", "ran"),
    [
        ({"reliability": "ok"}, True),
        ({"boot_time": "partial"}, True),
        ({"reliability": "empty", "boot_time": "ok"}, True),
        ({"reliability": "empty", "boot_time": "timeout"}, False),  # PowerShell printed nothing
        ({}, False),
    ],
)
def test_storage_source_historical_ran(status: dict, ran: bool) -> None:
    assert pipeline._storage_source(_trust(**status)) == {
        "state": "unavailable",
        "collector_status": "empty",
        "historical_ran": ran,
    }


def test_storage_source_without_its_row_is_no_hint() -> None:
    assert pipeline._storage_source(None) is None
    assert (
        pipeline._storage_source({"sources": {"reliability": {"collector_status": "ok"}}}) is None
    )


def test_untrusted_identity_gives_no_storage_source_hint() -> None:
    trust = {
        "sources": {
            "identity": {"state": "suspect"},
            "storage_reliability": {"state": "ok", "collector_status": "empty"},
            "reliability": {"state": "ok", "collector_status": "ok"},
        }
    }
    untrusted = compute_storage_risk(None, None, device_trust=pipeline._device_trust(trust))
    axis = pipeline._storage_axis(untrusted, {"disks": []}, trust)
    assert "storage_source" not in axis["source_lineage"]
    # an unconfirmed device cannot prove a SMART loss: the first episode pauses (H1)
    prev = {
        "state": "h3",
        "below_h": 8.0,
        "below_seen_at": (_T0 - timedelta(hours=4)).isoformat(),
        "worst_disk_id": "hA",
        "disk_ids": ["hA"],
        "had_disk": True,
    }
    v = compute_health({"storage_risk": axis}, {}, {}, None, None, prev, now=_T0)
    assert (v.state, v.blind_cause, v.below_h) == ("h3", "unknown", 8.0)
    # positive control: a trusted identity does carry the hint
    trusted = pipeline._storage_axis(compute_storage_risk(None, None), None, trust)
    assert trusted["source_lineage"]["storage_source"]["historical_ran"] is True


# --------------------------------------------------------------------------- #
# Spec H4 (P1-6): the events channel is delivered even when the journal is clean
# --------------------------------------------------------------------------- #
_NO_EVENTS = "нет анализа событий"
_ZERO = {"early": 0, "damage": 0, "crash": 0, "app_hang": 0}


@pytest.mark.parametrize(
    ("delivered", "counts", "live"),
    [
        (True, _ZERO, True),  # a clean journal: the agent sends EMPTY, the channel works
        (False, _ZERO, False),  # collector timeout and nothing counted: blind
        (None, _ZERO, False),  # rows stored before H4: as before
        (False, {**_ZERO, "damage": 3}, True),  # events seen: only adds liveness
    ],
)
def test_events_channel_is_live_when_delivered_or_seen(
    delivered: Optional[bool], counts: dict, live: bool
) -> None:
    chain = {
        "stage": 0,
        "burstiness": None,
        "recurrent_weeks": 0,
        "counts": counts,
        "factors": [],
        "delivered": delivered,
    }
    v = compute_health({"storage_risk": {"value": 0.0}}, {}, {}, chain, None, None)
    assert (_NO_EVENTS not in v.blind_spots) is live


def test_analyze_events_carries_delivered() -> None:
    from server.analytics.errchain import analyze_events

    assert analyze_events([], now=_T0, delivered=True).delivered is True
    assert analyze_events([], now=_T0).delivered is None


@pytest.mark.parametrize(
    ("status", "reason", "delivered"),
    [
        ("ok", None, True),
        ("empty", None, True),  # a clean journal
        ("empty", "rc_1", False),  # PowerShell printed nothing (review 3d)
        ("empty", "CouldNotAutoloadMatchingModule", False),  # broken PowerShell host
        ("partial", None, False),  # unparsable output: for events only a failed run
        ("timeout", None, False),
        ("blocked", None, False),
        (None, None, None),
    ],
)
def test_events_delivered_from_the_collector_status(
    status: Optional[str], reason: Optional[str], delivered: Optional[bool]
) -> None:
    row = {"collector_status": status, "collector_reason": reason}
    trust = {"sources": {"events": row}} if status else {"sources": {}}
    assert pipeline._events_delivered(trust) is delivered
    assert pipeline._events_delivered(None) is None


# --------------------------------------------------------------------------- #
# H7: horizon hysteresis -- tighten at once, loosen only past 1.25x the old bucket
# --------------------------------------------------------------------------- #
from server.analytics.health_hold import horizon  # noqa: E402


def _fill_trend(eta: Optional[float], direction: str = "worsening") -> dict:
    return {"disk_fill": {"direction": direction, "eta_days": eta}}


@pytest.mark.parametrize("edge", [30.0, 90.0, 180.0])  # 180: the edge to no horizon at all
def test_eta_noise_around_a_bucket_edge_flips_the_horizon_at_most_twice(edge: float) -> None:
    prev: Optional[dict] = None
    changes, last = 0, None
    for k in range(60 * 6):  # 60 days of 4 h cycles, ETA edge +- 4
        eta = edge + (4.0 if k % 2 else -4.0) * ((k % 7) / 6)
        days, _reason, kind = horizon("h0", _fill_trend(eta), prev=prev)
        changes += last is not None and days != last
        last, prev = days, {"horizon_days": days, "horizon_kind": kind}
    assert changes <= 2


def test_the_horizon_tightens_at_once_and_loosens_only_past_the_margin() -> None:
    prev = {"horizon_days": 30, "horizon_kind": "eta"}
    assert horizon("h0", _fill_trend(6.0), prev=prev)[0] == 7  # tighter: at once
    assert horizon("h0", _fill_trend(35.0), prev=prev)[0] == 30  # 35 <= 37.5: held
    assert horizon("h0", _fill_trend(40.0), prev=prev)[0] == 90  # past 1.25 x 30


def test_past_the_last_bucket_the_held_180_loosens_only_past_the_margin() -> None:
    prev = {"horizon_days": 180, "horizon_kind": "eta"}
    assert horizon("h0", _fill_trend(225.0), prev=prev)[0] == 180  # 225 <= 1.25 x 180: held
    assert horizon("h0", _fill_trend(226.0), prev=prev) == (None, "", None)
    assert horizon("h0", _fill_trend(200.0)) == (None, "", None)  # never shown without a hold


def test_no_forecast_now_drops_the_held_horizon_at_once() -> None:
    prev = {"horizon_days": 7, "horizon_kind": "eta"}
    assert horizon("h0", _fill_trend(None, "improving"), prev=prev) == (None, "", None)
    assert horizon("h2", {}, prev=prev)[0] == 90  # the state window, not the old 7
