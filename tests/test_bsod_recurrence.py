"""Spec H13 (P0-6): the BSOD rung by recurrence, the cause apart from it.

>= 2 distinct BugCheck days in 30 -> h2; >= 3 in 30 with >= 2 in the last 7 -> h3; never
h4 from BSODs alone. The cause (fleet / software / unknown) picks the action and never
moves the state; an untrusted identity has no rule; K5 keeps a hidden D at unknown.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest
from server.analytics.errchain import analyze_events
from server.analytics.health import _ACTIONS, compute_health
from server.analytics.stability import BSOD_ACTIONS, assess_stability, bsod_rung
from server.zabbix.items import _reason

from tests.test_calib_health import GATED, LIVE_CHAIN, MATURE, NOW, _axis, _st

_D = NOW.date()


def _days(*ago: int) -> list:
    return [(_D - timedelta(days=n)).isoformat() for n in ago]


@pytest.mark.parametrize(
    ("days", "rung"),
    [
        (_days(0), None),  # one day: an incident, not a pattern
        (_days(0, 0, 0), None),  # three crashes in one day are one BugCheck day
        (_days(0, 20), "h2"),
        (_days(1, 3, 20), "h3"),  # 3 in 30, 2 in the last 7
        (_days(1, 10, 20), "h2"),  # 3 in 30, only 1 in the last 7
        (_days(0, 1, 2, 3, 4, 5, 6), "h3"),  # never h4 from BSODs alone
        (_days(31, 35), None),  # outside the window
    ],
)
def test_the_bsod_rung_counts_distinct_days(days: list, rung) -> None:
    assert bsod_rung(days, NOW)[0] == rung


def _verdict(days: list, *, cohort=None, patch=None, axes=None, prev=None, now=NOW):
    stab = assess_stability(days, now, cohort=cohort, last_patch_date=patch)
    base = {"storage_risk": _st(10.0), "os_degradation_risk": _axis(20.0)}
    bayes = {"classes": [], "top": None, "overall": 0.0}
    return compute_health(
        axes or base, bayes, dict(MATURE), LIVE_CHAIN, None, prev, now=now, stability=stab
    )


def test_two_bsod_days_are_h2_with_the_unknown_cause_action() -> None:
    v = _verdict(_days(0, 20))
    assert (v.state, v.dominant, v.rung_source) == ("h2", "os", "bsod_days")
    assert (v.cause_class, v.action) == ("unknown", BSOD_ACTIONS["unknown"])
    assert any("BSOD" in (e.get("label") or "") for e in v.state_evidence)


def test_recurrent_bsods_are_h3() -> None:
    assert _verdict(_days(1, 3, 20)).state == "h3"


@pytest.mark.parametrize(
    ("cohort", "patch", "cause"),
    [
        ({"cohort_size": 8, "cohort_bsod_pct": 0.5, "cohort_bsod_n": 4}, None, "fleet_likely"),
        ({"cohort_size": 8, "cohort_bsod_pct": 0.2}, None, "unknown"),  # under 25 %
        ({"cohort_size": 4, "cohort_bsod_pct": 1.0}, None, "unknown"),  # too small a cohort
        (None, (_D - timedelta(days=22)).isoformat(), "software_likely"),  # 2 days before day 20
        (None, (_D - timedelta(days=25)).isoformat(), "unknown"),  # 5 days before: not it
    ],
)
def test_the_cause_picks_the_action_never_the_state(cohort, patch, cause: str) -> None:
    v = _verdict(_days(0, 20), cohort=cohort, patch=patch)
    assert (v.state, v.cause_class) == ("h2", cause)
    assert v.action == BSOD_ACTIONS[cause]


def test_the_disk_ladder_wins_a_bsod_tie_and_a_bsod_never_lowers_it() -> None:
    disk = {"storage_risk": _st(40.0, damage=40.0, rloss=30.0, flags=["pending_gt10"])}
    v = _verdict(_days(1, 3, 20), axes={**disk, "os_degradation_risk": _axis(20.0)})
    assert (v.state, v.dominant, v.rung_source) == ("h3", "storage", "flag:pending_gt10")


def test_a_hidden_damage_axis_keeps_unknown() -> None:
    # K5: the storage axis gated and no OS axis -> no Damage channel at all
    axes = {"storage_risk": _axis(None, lineage=GATED)}
    v = _verdict(_days(0, 20), axes=axes)
    assert v.state == "unknown" and v.rung_source is None
    # spec H13: the gate holds, the BSOD rung is visible only in the factors
    assert any("BSOD" in (f.get("label") or "") for f in v.factors)
    assert not any("BSOD" in (e.get("label") or "") for e in v.state_evidence)


def test_a_held_bsod_level_keeps_its_cause_and_action() -> None:
    first = _verdict(
        _days(0, 20), cohort={"cohort_size": 8, "cohort_bsod_pct": 0.5, "cohort_bsod_n": 4}
    )
    later = NOW + timedelta(days=11)  # the older day left the window: no rung, the h2 is held
    v = _verdict(_days(0, 20), prev=dataclasses.asdict(first), now=later)
    assert (v.state, v.rung_source, v.dominant) == ("h2", "hold", "os")
    assert (v.cause_class, v.action) == ("fleet_likely", BSOD_ACTIONS["fleet_likely"])


def test_bugcheck_days_merge_the_raw_window_and_the_rollups() -> None:
    def bc(at: datetime) -> dict:
        stamp = at.strftime("%Y-%m-%dT%H:%M:%S.0000000Z")
        received = (at + timedelta(minutes=5)).isoformat()
        return {
            "log": "System",
            "source": "Microsoft-Windows-WER-SystemErrorReporting",
            "event_id": 1001,
            "level": 2,
            "ts": stamp,
            "received_at": received,
        }

    now = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)
    events = [
        bc(now - timedelta(hours=1)),
        bc(now - timedelta(hours=2)),
        bc(now - timedelta(days=3)),
    ]
    rollups = [
        {
            "day": "2026-10-10",
            "event_key": "Microsoft-Windows-WER-SystemErrorReporting:1001",
            "n": 1,
        },
        {
            "day": "2026-09-01",
            "event_key": "Microsoft-Windows-WER-SystemErrorReporting:1001",
            "n": 2,
        },
        {"day": "2026-10-11", "event_key": "disk:7", "n": 5},
    ]
    chain = analyze_events(events, now=now, rollup_counts=rollups)
    assert chain.bugcheck_days == ["2026-10-10", "2026-10-27", "2026-10-30"]


def test_one_bugcheck_across_utc_midnight_is_one_day() -> None:
    """store_events folds a row into the rollup of its RECEIPT day, the raw row is dated by
    its own ts -- one crash stays one day (no false h2); a day the raw window lost counts."""
    key = "Microsoft-Windows-WER-SystemErrorReporting:1001"
    now = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
    late = {
        "log": "System",
        "source": "Microsoft-Windows-WER-SystemErrorReporting",
        "event_id": 1001,
        "level": 2,
        "ts": "2026-10-30T21:00:00.0000000Z",
        "received_at": "2026-10-31T00:05:00+00:00",
    }
    ingest = {"day": "2026-10-31", "event_key": key, "n": 1}
    chain = analyze_events([late], now=now, rollup_counts=[ingest])
    assert chain.bugcheck_days == ["2026-10-30"]
    assert assess_stability(chain.bugcheck_days, now) is None
    lost = {"day": "2026-10-12", "event_key": key, "n": 1}  # pruned from the raw window
    chain = analyze_events([late], now=now, rollup_counts=[ingest, lost])
    assert chain.bugcheck_days == ["2026-10-12", "2026-10-30"]


def test_bsod_actions_have_no_colon() -> None:
    """D7: Zabbix joins «label: action» (server/zabbix/items.py) -- an action has no colon."""
    assert [a for a in BSOD_ACTIONS.values() if ":" in a] == []
    v = _verdict(_days(0, 20))  # h2, cause unknown -- the usual case before agent 1.0.0
    reason = _reason({"dominant_label": v.dominant_label, "action": v.action}, v.state)
    assert reason.count(":") == 1


def test_a_held_bsod_cohort_cause_is_no_disk_cohort() -> None:
    """H13's fleet_likely (a BSOD cohort) held through a K5-gated cycle whose live chain
    names the disk; the model has no disk errors -> the storage action, never H14's."""
    cohort = {"cohort_size": 8, "cohort_bsod_pct": 0.5, "cohort_bsod_n": 4}
    first = _verdict(_days(1, 3, 20), cohort=cohort)
    counts = {"early": 0, "damage": 3, "crash": 0, "app_hang": 0}
    lin = {"cohort_size": 8, "cohort_disk_err_n": 0, "cohort_disk_err_pct": 0.0}
    axes = {
        "storage_risk": _axis(None, lineage=GATED),
        "fleet_anomaly_risk": _axis(10.0, lineage=lin),
    }
    bayes = {"classes": [], "top": None, "overall": 0.0}
    chain = {**LIVE_CHAIN, "stage": 2, "counts": counts}
    prev = dataclasses.asdict(first)
    v = compute_health(axes, bayes, dict(MATURE), chain, None, prev, now=NOW + timedelta(hours=4))
    assert (v.state, v.dominant, v.held_source) == ("h3", "storage", "bsod_days")
    assert v.cause_class == "fleet_likely"  # the BSOD cause the held level was set with
    assert v.action == _ACTIONS["storage"]
