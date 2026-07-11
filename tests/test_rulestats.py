"""ssd3 Ф8 (Task 1): rule_stats table + scan_device()/reinforcement() detection
and application math, plus the run_rulestats_scan maintenance-sweep wiring.

scan_device/reinforcement tests are pure-function (no DB). The
get_score_series(since=...)/run_rulestats_scan/record_rule_outcomes tests go
through server.db, pure SQLite -- no network, no FastAPI.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from server.analytics import rulestats

pytestmark = pytest.mark.unit


@pytest.fixture
def db_init(tmp_path):
    from server import db

    db.init_db(tmp_path / "t.db")
    return db


# --------------------------------------------------------------------------- #
# Fixture builders -- match pipeline.py's real risk-block shape exactly
# (score100.storage_risk.{band,coords.flags}, errchain.stage).
# --------------------------------------------------------------------------- #
def _risk(flags=None, band="good", stage=0):
    return {
        "score100": {"storage_risk": {"band": band, "coords": {"flags": list(flags or [])}}},
        "errchain": {"stage": stage},
    }


def _row(ts, flags=None, band="good", stage=0):
    """A score_rows entry, shaped like get_score_series' return value."""
    ts_str = ts.isoformat() if hasattr(ts, "isoformat") else ts
    return {"ts": ts_str, "risk": _risk(flags=flags, band=band, stage=stage)}


def _seed_score(db, device_id, ts, flags=None, band="good", stage=0):
    db.store_scores(device_id, ts, {"risk": _risk(flags=flags, band=band, stage=stage)})


# --------------------------------------------------------------------------- #
# scan_device -- episode detection + confirm/refute/unresolved resolution
# --------------------------------------------------------------------------- #
def test_scan_device_confirmed_episode():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        _row(t0, flags=["pending_gt10"]),
        _row(t0 + timedelta(days=1), flags=["pending_gt10"]),
        _row(t0 + timedelta(days=2), flags=[]),  # closes the run; end_ts = t0+1d
        _row(t0 + timedelta(days=10), flags=[], band="bad"),  # within 45d -> confirms
    ]
    outcomes = rulestats.scan_device(rows, now=t0 + timedelta(days=20))
    assert ("pending_high", "confirmed") in outcomes


def test_scan_device_refuted_episode():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        _row(t0, flags=["recurrence"]),
        _row(t0 + timedelta(days=1), flags=[]),  # closes the run; end_ts = t0
    ]
    outcomes = rulestats.scan_device(rows, now=t0 + timedelta(days=61))
    assert ("media_recurrence", "refuted") in outcomes


def test_scan_device_unresolved_episode_emits_nothing():
    """Too recent for either outcome window -> omitted entirely, not in either list."""
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        _row(t0, flags=["early_events"]),
        _row(t0 + timedelta(days=1), flags=[]),  # closes; end_ts = t0
    ]
    outcomes = rulestats.scan_device(rows, now=t0 + timedelta(days=10))
    assert outcomes == []


def test_scan_device_resolves_on_later_call_with_more_rows():
    """Same conceptual episode: unresolved with a short window/now, resolves once
    more rows and more elapsed time are available -- proves statelessness, not
    that the function needs internal memory (each call recomputes from scratch)."""
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        _row(t0, flags=["early_events"]),
        _row(t0 + timedelta(days=1), flags=[]),  # closes; end_ts = t0
    ]
    first = rulestats.scan_device(rows, now=t0 + timedelta(days=10))
    assert first == []

    later_rows = rows + [_row(t0 + timedelta(days=65), flags=[])]
    second = rulestats.scan_device(later_rows, now=t0 + timedelta(days=65))
    assert ("early_chain", "refuted") in second


def test_scan_device_unparseable_ts_is_treated_as_flag_absent_not_a_crash():
    """A row whose "ts" fails to parse must never raise -- treated as flag-absent,
    which lets it close an otherwise-open run just like a genuine flag-absent row
    (even though this malformed row's own flags still list the rule's flag)."""
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        _row(t0, flags=["pending_gt10"]),
        {"ts": "not-a-timestamp", "risk": _risk(flags=["pending_gt10"])},  # closes; end_ts = t0
    ]
    now = t0 + rulestats.REFUTE_WINDOW  # must not raise
    outcomes = rulestats.scan_device(rows, now=now)
    assert ("pending_high", "refuted") in outcomes


# --------------------------------------------------------------------------- #
# Open-episode: locks in the Замечание №3 fix -- a run still flag-present at
# the last row is a data-horizon artifact, NOT a finished episode.
# --------------------------------------------------------------------------- #
def test_scan_device_open_episode_at_end_of_rows_is_not_synthesized_closed():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        _row(t0, flags=["pending_gt10"]),
        _row(t0 + timedelta(days=1), flags=["pending_gt10"]),  # still open at the last row
    ]
    # far enough past that a synthesized end_ts=t0+1d would already be >=60d old
    now = t0 + timedelta(days=100)
    outcomes = rulestats.scan_device(rows, now=now)
    assert not any(rule_key == "pending_high" for rule_key, _ in outcomes)

    closed_rows = rows + [_row(t0 + timedelta(days=2), flags=[])]  # flag finally absent
    outcomes2 = rulestats.scan_device(closed_rows, now=now)
    assert ("pending_high", "refuted") in outcomes2


# --------------------------------------------------------------------------- #
# REFUTE_WINDOW boundary: >= on the wait-gate, inclusive (end_ts, end_ts+window]
# on the evidence scan -- both edges pinned exactly, not "close enough".
# --------------------------------------------------------------------------- #
def test_scan_device_refute_boundary_is_inclusive_at_exactly_refute_window():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        _row(t0, flags=["recurrence"]),
        _row(t0 + timedelta(days=1), flags=[]),  # closes; end_ts = t0
    ]
    now = t0 + rulestats.REFUTE_WINDOW  # now - end_ts == REFUTE_WINDOW exactly
    outcomes = rulestats.scan_device(rows, now=now)
    assert ("media_recurrence", "refuted") in outcomes


def test_scan_device_refute_window_upper_bound_is_inclusive_counter_evidence():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end_ts = t0
    rows = [
        _row(t0, flags=["recurrence"]),
        _row(t0 + timedelta(days=1), flags=[]),  # closes; end_ts = t0
        _row(end_ts + rulestats.REFUTE_WINDOW, flags=["recurrence"]),  # re-fire at the closed edge
    ]
    now = end_ts + rulestats.REFUTE_WINDOW + timedelta(days=5)
    outcomes = rulestats.scan_device(rows, now=now)
    assert outcomes == []  # blocked: re-fire lands exactly on the inclusive upper bound


# --------------------------------------------------------------------------- #
# Dedup (since): an already-emitted episode must not be re-emitted.
# --------------------------------------------------------------------------- #
def test_scan_device_episode_not_doubled_when_since_matches_prior_end_ts():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [
        _row(t0, flags=["pending_gt10"]),
        _row(t0 + timedelta(days=1), flags=["pending_gt10"]),
        _row(t0 + timedelta(days=2), flags=[]),  # closes; end_ts = t0+1d
        _row(t0 + timedelta(days=10), flags=[], band="bad"),  # confirms
    ]
    now = t0 + timedelta(days=20)
    first = rulestats.scan_device(rows, now=now)
    assert ("pending_high", "confirmed") in first

    end_ts = t0 + timedelta(days=1)
    second = rulestats.scan_device(rows, now=now, since=end_ts)
    assert not any(rule_key == "pending_high" for rule_key, _ in second)


# --------------------------------------------------------------------------- #
# reinforcement: bounded [0.8..1.5] multiplier, first-match-wins thresholds.
# --------------------------------------------------------------------------- #
def test_reinforcement_below_boost_threshold_ratio_irrelevant():
    assert rulestats.reinforcement("pending_high", {"confirmed": 4, "refuted": 0}) == 1.0


def test_reinforcement_boost_at_confirmed_count_boundary():
    # confirmed exactly at the inclusive minimum (5); ratio comfortably above 0.7.
    assert rulestats.reinforcement("pending_high", {"confirmed": 5, "refuted": 0}) == 1.2


def test_reinforcement_boost_at_ratio_boundary():
    # confirmed=7/refuted=3 -> ratio is exactly the float 0.7 (7/10 == 0.7 bit-for-bit);
    # confirmed=5 simultaneously with ratio=0.7 exactly is not reachable with integer
    # counts (5/0.7 is not an integer total), so the two >= boundaries are pinned in
    # separate tests instead of forcing an unreachable combination.
    assert rulestats.reinforcement("pending_high", {"confirmed": 7, "refuted": 3}) == 1.2


def test_reinforcement_ceiling_at_confirmed_count_boundary():
    assert rulestats.reinforcement("pending_high", {"confirmed": 15, "refuted": 0}) == 1.5


def test_reinforcement_high_confirmed_but_low_ratio_is_neutral_not_ceiling():
    # confirmed=15 alone would qualify for the ceiling's count check, but ratio<0.7
    # fails BOTH the ceiling's and the boost's own ratio requirement -> falls all the
    # way through to neutral (1.0), not boost (1.2).
    assert rulestats.reinforcement("pending_high", {"confirmed": 15, "refuted": 10}) == 1.0


def test_reinforcement_mute_below_ratio_ceiling():
    assert rulestats.reinforcement("pending_high", {"confirmed": 4, "refuted": 10}) == 0.8


def test_reinforcement_ratio_exactly_at_mute_ceiling_is_not_muted():
    # confirmed=6/refuted=14 -> ratio is exactly the float 0.3 (6/20 == 0.3); the mute
    # gate is a strict "<", so exactly-0.3 must fall through to neutral, not mute.
    assert rulestats.reinforcement("pending_high", {"confirmed": 6, "refuted": 14}) == 1.0


def test_reinforcement_empty_stats_is_exactly_neutral():
    assert rulestats.reinforcement("pending_high", {}) == 1.0
    assert rulestats.reinforcement("early_chain", {}) == 1.0
    assert rulestats.reinforcement("media_recurrence", {}) == 1.0


def test_reinforcement_thresholds_identical_across_rule_keys():
    """rule_key is accepted for signature symmetry only -- never branched on."""
    stats = {"confirmed": 7, "refuted": 3}
    results = {rk: rulestats.reinforcement(rk, stats) for rk in rulestats.RULE_KEYS}
    assert len(set(results.values())) == 1


# --------------------------------------------------------------------------- #
# get_score_series(..., since=...)
# --------------------------------------------------------------------------- #
def test_get_score_series_since_filters_older_rows(db_init):
    db = db_init
    old_ts = "2026-01-01T00:00:00+00:00"
    new_ts = "2026-03-25T00:00:00+00:00"  # > 70 days after old_ts
    _seed_score(db, "dev-1", old_ts)
    _seed_score(db, "dev-1", new_ts)

    since = "2026-02-01T00:00:00+00:00"
    filtered = db.get_score_series("dev-1", since=since)
    assert [r["ts"] for r in filtered] == [new_ts]

    unfiltered = db.get_score_series("dev-1")
    assert len(unfiltered) == 2  # existing no-since callers see exact prior behavior


# --------------------------------------------------------------------------- #
# run_rulestats_scan -- fleet sweep integration (through db_init)
# --------------------------------------------------------------------------- #
def test_run_rulestats_scan_records_and_is_idempotent_on_repeat(db_init):
    db = db_init
    now = datetime.now(timezone.utc)

    def iso(days_ago):
        return (now - timedelta(days=days_ago)).isoformat()

    db.upsert_device("dev-confirm", iso(0), "1.0.0")
    db.upsert_device("dev-refute", iso(0), "1.0.0")

    # dev-confirm: early_chain flag run closes, a bad-band row confirms it within 45d.
    _seed_score(db, "dev-confirm", iso(40), flags=["early_events"])
    _seed_score(db, "dev-confirm", iso(39), flags=[])
    _seed_score(db, "dev-confirm", iso(35), flags=[], band="bad")

    # dev-refute: media_recurrence flag run closes, then 60+ clean days follow.
    _seed_score(db, "dev-refute", iso(65), flags=["recurrence"])
    _seed_score(db, "dev-refute", iso(64), flags=[])

    result = db.run_rulestats_scan()
    assert result["devices_scanned"] == 2
    assert result["confirmed"] == 1
    assert result["refuted"] == 1

    stats = db.get_rule_stats()
    assert stats["early_chain"] == {"confirmed": 1, "refuted": 0}
    assert stats["media_recurrence"] == {"confirmed": 0, "refuted": 1}

    # idempotent repeat: no new data -> counts unchanged (since wiring works end to end).
    result2 = db.run_rulestats_scan()
    assert result2["confirmed"] == 0
    assert result2["refuted"] == 0
    assert db.get_rule_stats() == stats


# --------------------------------------------------------------------------- #
# get_rule_stats / record_rule_outcomes
# --------------------------------------------------------------------------- #
def test_get_rule_stats_empty_when_no_rows(db_init):
    assert db_init.get_rule_stats() == {}


def test_record_rule_outcomes_aggregates_duplicates_and_upserts_across_calls(db_init):
    db = db_init
    db.record_rule_outcomes(
        [
            ("pending_high", "confirmed"),
            ("pending_high", "confirmed"),
            ("pending_high", "refuted"),
        ]
    )
    assert db.get_rule_stats()["pending_high"] == {"confirmed": 2, "refuted": 1}

    db.record_rule_outcomes([("pending_high", "confirmed")])
    assert db.get_rule_stats()["pending_high"] == {"confirmed": 3, "refuted": 1}


def test_record_rule_outcomes_rejects_unknown_rule_key(db_init):
    with pytest.raises(ValueError):
        db_init.record_rule_outcomes([("not_a_real_rule", "confirmed")])


def test_record_rule_outcomes_rejects_unknown_outcome(db_init):
    with pytest.raises(ValueError):
        db_init.record_rule_outcomes([("pending_high", "maybe")])
