"""ssd3 Ф3 (T3.2/T3.4): errchain -- deterministic event-chain escalation
detector. Pure function; time is anchored on received_at only (the agent's
own ``ts`` is never trusted -- clocks drift/lie), matching the rest of the
ssd3 series (K3: observations, never invented causality).
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest
from server.analytics.errchain import analyze_events

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)


def _event(source, event_id, days_ago, **kw):
    ts = _NOW - timedelta(days=days_ago)
    base = {"source": source, "event_id": event_id, "received_at": ts.isoformat(), "level": "Error"}
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# Empty input / shape
# --------------------------------------------------------------------------- #


def test_empty_input_is_stage_zero_and_no_crash():
    chain = analyze_events([], now=_NOW)
    assert chain.stage == 0
    assert chain.burstiness is None
    assert chain.recurrent_weeks == 0
    assert chain.counts == {"early": 0, "damage": 0, "crash": 0, "app_hang": 0}
    assert chain.factors == []


def test_errchain_is_asdict_serializable():
    chain = analyze_events([], now=_NOW)
    d = asdict(chain)
    assert set(d) == {"stage", "burstiness", "recurrent_weeks", "counts", "factors"}


# --------------------------------------------------------------------------- #
# Stages 0/1/2/3
# --------------------------------------------------------------------------- #


def test_stage_1_retries_only():
    chain = analyze_events([_event("disk", 153, 1)], now=_NOW)
    assert chain.stage == 1
    assert chain.counts["early"] == 1
    assert chain.counts["damage"] == 0


def test_stage_2_retries_and_damage():
    events = [_event("disk", 153, 5), _event("Ntfs", 55, 2)]
    chain = analyze_events(events, now=_NOW)
    assert chain.stage == 2
    assert chain.counts == {"early": 1, "damage": 1, "crash": 0, "app_hang": 0}


def test_stage_2_damage_without_any_early_still_counts_as_damage():
    """A damage event with no logged retry beforehand must not silently
    collapse to stage 0 -- damage is never dropped for lack of a precursor
    (К7: never lose a real damage signal)."""
    chain = analyze_events([_event("disk", 7, 3)], now=_NOW)
    assert chain.stage == 2


def test_stage_3_damage_then_crash_within_7_days():
    events = [_event("disk", 51, 10), _event("System", 41, 5)]  # damage day10, crash day5 (5d gap)
    chain = analyze_events(events, now=_NOW)
    assert chain.stage == 3


def test_stage_not_3_when_crash_more_than_7_days_after_damage():
    events = [_event("disk", 51, 25), _event("System", 6008, 5)]  # 20-day gap
    chain = analyze_events(events, now=_NOW)
    assert chain.stage == 2


def test_stage_not_3_when_crash_precedes_damage():
    events = [_event("System", 41, 20), _event("disk", 7, 5)]  # crash BEFORE damage
    chain = analyze_events(events, now=_NOW)
    assert chain.stage == 2


def test_crash_alone_without_damage_does_not_reach_stage_3():
    chain = analyze_events([_event("System", 6008, 2)], now=_NOW)
    assert chain.stage == 0
    assert chain.counts["crash"] == 1


# --------------------------------------------------------------------------- #
# 30-day window + received_at-only time anchor
# --------------------------------------------------------------------------- #


def test_events_outside_30_day_window_are_ignored():
    chain = analyze_events([_event("disk", 153, 40)], now=_NOW)
    assert chain.stage == 0
    assert chain.counts["early"] == 0


def test_time_uses_received_at_not_ts():
    ev = {
        "source": "disk",
        "event_id": 153,
        "level": "Error",
        "ts": (_NOW - timedelta(days=99)).isoformat(),  # garbage/stale agent clock
        "received_at": (_NOW - timedelta(days=1)).isoformat(),  # fresh server receipt
    }
    chain = analyze_events([ev], now=_NOW)
    assert chain.stage == 1  # driven by received_at, not the stale ts


def test_missing_received_at_is_ignored_even_with_fresh_ts():
    ev = {"source": "disk", "event_id": 153, "level": "Error", "ts": _NOW.isoformat()}
    chain = analyze_events([ev], now=_NOW)
    assert chain.stage == 0


# --------------------------------------------------------------------------- #
# recurrent_weeks
# --------------------------------------------------------------------------- #


def test_recurrent_weeks_counts_distinct_iso_weeks():
    events = [_event("disk", 153, 1), _event("disk", 153, 8), _event("disk", 153, 15)]
    chain = analyze_events(events, now=_NOW)
    assert chain.recurrent_weeks == 3  # each pair 7 days apart -> always a different ISO week


def test_recurrent_weeks_zero_for_single_event():
    chain = analyze_events([_event("disk", 153, 1)], now=_NOW)
    assert chain.recurrent_weeks == 1


# --------------------------------------------------------------------------- #
# burstiness
# --------------------------------------------------------------------------- #


def test_burstiness_none_below_four_events():
    events = [_event("disk", 153, d) for d in (1, 2, 3)]
    chain = analyze_events(events, now=_NOW)
    assert chain.burstiness is None


def test_burstiness_high_for_clustered_events():
    # One lone event, then a tight cluster of 5 one minute apart: gaps are
    # [~huge, 60s, 60s, 60s, 60s] (5 gaps) -> for this "one dominant gap, rest
    # near-zero" shape sample stdev/mean converges to sqrt(num_gaps) = sqrt(5)
    # ~= 2.24, safely over the >2 clustering threshold (a 3-gap version of
    # this shape only reaches sqrt(3) ~= 1.73, which is why >=4 events alone
    # isn't enough headroom here -- need the extra cluster point).
    lone = _NOW - timedelta(days=25)
    cluster = _NOW - timedelta(days=1)
    events = [
        _event("disk", 153, 0, received_at=lone.isoformat()),
        _event("disk", 153, 0, received_at=cluster.isoformat()),
        _event("disk", 153, 0, received_at=(cluster + timedelta(minutes=1)).isoformat()),
        _event("disk", 153, 0, received_at=(cluster + timedelta(minutes=2)).isoformat()),
        _event("disk", 153, 0, received_at=(cluster + timedelta(minutes=3)).isoformat()),
        _event("disk", 153, 0, received_at=(cluster + timedelta(minutes=4)).isoformat()),
    ]
    chain = analyze_events(events, now=_NOW)
    assert chain.burstiness is not None
    assert chain.burstiness > 2


def test_burstiness_low_for_roughly_even_gaps():
    events = [_event("disk", 153, d) for d in (1, 8, 15, 22)]  # ~7 days apart each
    chain = analyze_events(events, now=_NOW)
    assert chain.burstiness is not None
    assert chain.burstiness < 1.0


# --------------------------------------------------------------------------- #
# app_hang (bayes-only, not part of the chain) + unclassified ids (K8)
# --------------------------------------------------------------------------- #


def test_app_hang_counted_separately_and_never_escalates_the_chain():
    events = [_event("Application", 1002, 1), _event("Application", 1002, 2)]
    chain = analyze_events(events, now=_NOW)
    assert chain.counts["app_hang"] == 2
    assert chain.stage == 0


def test_unclassified_event_id_is_ignored():
    """disk/157 is collected by the agent (T3.1) but has no coordinate
    announced for it yet (К8) -- must be silently ignored, not guessed at."""
    chain = analyze_events([_event("disk", 157, 1)], now=_NOW)
    assert chain.stage == 0
    assert chain.counts == {"early": 0, "damage": 0, "crash": 0, "app_hang": 0}


def test_unrecognized_source_for_a_storage_id_does_not_count():
    """153/129/55/7/51 only count as storage evidence from the whitelisted
    storage providers (client/collectors/events.py T3.1); a coincidental id
    match from an unrelated provider must not be attributed to storage."""
    chain = analyze_events([_event("SomeOtherProvider", 153, 1)], now=_NOW)
    assert chain.counts["early"] == 0
    assert chain.stage == 0


@pytest.mark.parametrize("bad_id", [None, "not-a-number", ""])
def test_malformed_event_id_is_ignored_not_raised(bad_id):
    ev = {"source": "disk", "event_id": bad_id, "received_at": _NOW.isoformat(), "level": "Error"}
    chain = analyze_events([ev], now=_NOW)
    assert chain.stage == 0
