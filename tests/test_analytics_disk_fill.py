"""W4.2 disk-fill / servicing-collapse engine: free-space danger + WU confirmation.

The spec (cctodo W4.2): a deterministic forecast of system free-space depletion and
the downstream Windows-servicing/update collapse it causes. This engine is the
*current-state* verdict (the depletion *slope/ETA* lives in the W4.1 trajectory
engine), and its defining job is to **distinguish a cleanup rebound from true
depletion**: a Windows-Update staging dip that recovers must not raise an alarm,
while a drive that sits persistently full must. We do that by grading on the
*median* of the recent free-space window -- a one-off dip cannot move the median.

Servicing failures (Microsoft-Windows-WindowsUpdateClient install/download
failures) are the downstream *confirmation*: with a low disk they confirm the fill
is breaking servicing; with a healthy disk, repeated failures still flag a real
"machine not patching" risk (cause uncertain). Gating mirrors W0.5/W4.1: untrusted
withholds; no free-space data and no servicing signal -> UNKNOWN (never a confident
zero); a directly-measured healthy drive -> a confident all-clear.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from server.analytics.disk_fill import compute_disk_fill_risk

_WU = "Microsoft-Windows-WindowsUpdateClient"


def _series(*free_space_pcts):
    """Recent heartbeat rows, newest-first (as the DB returns them)."""
    return [{"free_space_pct": v} for v in free_space_pcts]


def _at(free, age_days):
    """A heartbeat row stamped ``age_days`` ago (server-receipt time)."""
    when = (datetime.now(timezone.utc) - timedelta(days=age_days)).isoformat()
    return {"free_space_pct": free, "received_at": when}


def _wu_failures(n, event_id=20):
    return [{"source": _WU, "event_id": event_id} for _ in range(n)]


# --------------------------------------------------------------------------- #
# Gating
# --------------------------------------------------------------------------- #
def test_no_data_at_all_is_unknown():
    # No free-space telemetry and no servicing signal -> we know nothing.
    s = compute_disk_fill_risk([], [])
    assert s.value is None
    assert s.confidence == "unknown"


def test_untrusted_device_withholds():
    s = compute_disk_fill_risk(_series(4, 5, 4), _wu_failures(3), device_trust="untrusted")
    assert s.value is None
    assert s.confidence == "unknown"


def test_ample_free_space_is_confident_zero():
    # Free space is directly measured, so a healthy drive is a confident all-clear
    # (unlike the battery engine's swelling blind spot).
    s = compute_disk_fill_risk(_series(50, 55, 52, 48), [])
    assert s.value == 0.0
    assert s.confidence == "high"
    assert s.band == "good"
    assert s.direction == "higher_is_worse"


# --------------------------------------------------------------------------- #
# Free-space depletion (current-state, rebound-robust)
# --------------------------------------------------------------------------- #
def test_persistently_low_free_space_is_high_risk():
    s = compute_disk_fill_risk(_series(5, 6, 4, 5, 6), [])
    assert s.value is not None and s.value >= 40  # bad band
    assert s.direction == "higher_is_worse"


def test_cleanup_rebound_is_not_alarmed():
    """THE headline: a single fresh dip (latest reading low) among healthy samples
    is a Windows-Update cleanup rebound, not depletion -- the median stays healthy
    so we do NOT raise risk, but we surface the dip for the operator."""
    s = compute_disk_fill_risk(_series(6, 40, 42, 39, 41), [])  # newest = 6, a transient dip
    assert s.value == 0.0
    # the dip is not hidden -- it is noted as a possible transient.
    joined = " ".join(s.missing_evidence).lower()
    assert "transient" in joined or "not persistent" in joined


def test_critical_free_space_reads_severe():
    s = compute_disk_fill_risk(_series(1.0, 1.5, 1.2), [])
    assert s.value is not None and s.value >= 55


def test_low_free_space_is_a_watch_not_yet_bad():
    # 10-15% free: low and worth watching, but not yet the bad band.
    s = compute_disk_fill_risk(_series(12, 13, 11), [])
    assert s.value is not None and 15 <= s.value < 40
    assert s.band == "watch"


def test_getting_full_is_a_mild_watch():
    # 15-20% free: below the ~20% headroom Windows wants -> mild watch.
    s = compute_disk_fill_risk(_series(18, 19, 17), [])
    assert s.value is not None and 15 <= s.value < 40


def test_single_low_sample_is_reported_but_lower_confidence():
    # One low reading is a present danger (graded), but we cannot yet confirm it is
    # persistent rather than a transient -> medium confidence, not high.
    s = compute_disk_fill_risk(_series(5), [])
    assert s.value is not None and s.value >= 40
    assert s.confidence == "medium"


def test_critically_full_with_enough_samples_is_high_confidence():
    # Free space is directly measured -> a persistently critical drive is high
    # confidence once we have enough samples to rule out a transient.
    s = compute_disk_fill_risk(_series(2, 1.5, 2, 1), [])
    assert s.value is not None and s.value >= 55
    assert s.confidence == "high"


def test_stale_low_samples_outside_recency_window_are_ignored():
    # A drive that was full months ago but has been clean recently must read as a
    # current-state all-clear -- the stale lows are outside the recency window and
    # must not drag the median into a false depletion alarm (prime directive: no
    # false alarms). Newest readings healthy; the lows are >2 weeks old.
    series = [_at(50, 0), _at(52, 1), _at(4, 30), _at(4, 33), _at(4, 36), _at(4, 40)]
    s = compute_disk_fill_risk(series, [])
    assert s.value == 0.0


# --------------------------------------------------------------------------- #
# Servicing-collapse confirmation
# --------------------------------------------------------------------------- #
def test_servicing_failures_amplify_a_low_disk():
    low_only = compute_disk_fill_risk(_series(4, 5, 4, 5), [])
    low_plus_wu = compute_disk_fill_risk(_series(4, 5, 4, 5), _wu_failures(4))
    assert low_plus_wu.value > low_only.value


def test_servicing_collapse_standalone_when_disk_is_ok():
    # Healthy disk but updates repeatedly failing -> a real "not patching" risk,
    # cause uncertain (a partition we do not measure, or update corruption).
    s = compute_disk_fill_risk(_series(50, 52, 48), _wu_failures(4))
    assert s.value is not None and 15 <= s.value < 40  # watch band, not critical
    assert s.confidence == "medium"  # cause uncertain
    labels = " ".join(f["label"].lower() for f in s.factors)
    assert "update" in labels or "servicing" in labels or "patch" in labels


def test_a_single_update_failure_on_a_healthy_disk_is_ignored():
    # Updates fail transiently all the time; one failure is not a collapse.
    s = compute_disk_fill_risk(_series(50, 52, 48), _wu_failures(1))
    assert s.value == 0.0


def test_servicing_collapse_without_free_space_data_is_medium():
    # No free-space telemetry, but updates clearly failing -> report at medium
    # confidence and flag the missing free-space observation.
    s = compute_disk_fill_risk([], _wu_failures(4))
    assert s.value is not None and s.value > 0
    assert s.confidence == "medium"
    joined = " ".join(s.missing_evidence).lower()
    assert "free space" in joined or "free-space" in joined


def test_non_windowsupdate_event_id_20_is_not_a_servicing_failure():
    # Event ID 20 from another provider (e.g. "disk") must not be miscounted as a
    # Windows Update failure -- matching is by provider, not bare numeric id.
    other = [{"source": "disk", "event_id": 20} for _ in range(5)]
    s = compute_disk_fill_risk(_series(50, 52, 48), other)
    assert s.value == 0.0


def test_malformed_events_do_not_crash_or_false_trigger():
    # A non-dict row, a None source, and a non-numeric id must be skipped, not
    # miscounted -- never invent a servicing collapse from junk.
    junk = ["nonsense", {"source": None, "event_id": 20}, {"source": _WU, "event_id": "x"}]
    s = compute_disk_fill_risk(_series(50, 52, 48), junk)
    assert s.value == 0.0
    assert s.source_lineage.get("servicing_failures") == 0


# --------------------------------------------------------------------------- #
# Honesty: blind spots, explainability, lineage
# --------------------------------------------------------------------------- #
def test_blind_spots_always_flagged_on_a_verdict():
    s = compute_disk_fill_risk(_series(5, 6, 4), [])
    joined = " ".join(s.missing_evidence).lower()
    assert "system" in joined and "drive" in joined  # only system-drive observed
    assert "servicing" in joined or "update" in joined  # servicing inferred from events


def test_factors_explain_the_verdict():
    s = compute_disk_fill_risk(_series(3, 4, 3), [])
    assert s.factors
    labels = " ".join(f["label"].lower() for f in s.factors)
    assert "free" in labels or "disk" in labels or "full" in labels


def test_all_clear_reason_is_explained():
    s = compute_disk_fill_risk(_series(50, 55, 52), [])
    assert s.value == 0.0
    assert "ample" in s.reason.lower() or "free space" in s.reason.lower()


def test_lineage_exposes_current_and_typical_and_counts():
    s = compute_disk_fill_risk(_series(6, 7, 5, 6), _wu_failures(4))
    lin = s.source_lineage
    assert lin.get("free_space_current") == 6
    assert lin.get("free_space_typical") is not None
    assert lin.get("n_samples") == 4
    assert lin.get("servicing_failures") == 4
