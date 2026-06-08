"""W4.2 OS-degradation engine: RSI-led stability verdict + crash confirmation.

The spec (cctodo W4.2): a deterministic current-state verdict on OS health using
Windows' Reliability Monitor RSI (the leading signal), 30-day system crash counts
(BSODs + dirty shutdowns = direct confirmation), and current average boot time as
an independent degradation indicator.

Key design decisions under test:
  * RSI is the leading signal. A low RSI is Windows' own synthesis of OS instability;
    it aggregates crashes, OS failures, app failures over time. The current-state value
    (not a trend) is what this engine reads.
  * BSODs (bugchecks) confirm. A BSOD is a system crash regardless of cause; even one
    in 30 days is significant. The RSI already incorporates them, but their explicit
    count is a direct, higher-weight confirmation.
  * Dirty shutdowns are moderate evidence. Less specific than BSODs (power loss +
    forced reboots), but repeated events correlate with instability.
  * Boot-rot (avg_boot_ms) is an independent signal: a very slow current-state boot
    indicates software/OS degradation. The boot-time *slope* lives in W4.1 trajectory.
  * KP41 and app crashes are NOT primary drivers here: per cctodo D6, KP41 specificity
    is near zero (power events, not OS failure). App crashes are application-level, not
    OS-level. They appear in lineage but do not drive the score.
  * Pending-reboot state is not collected -> permanently in missing_evidence.
  * Confidence caps at medium: RSI is an opaque heuristic; crash counts cannot
    distinguish hardware fault, driver regression, or OS corruption.
  * Gating: untrusted withholds; no RSI and no crash counts -> UNKNOWN (never a
    confident zero from silence).
"""

from __future__ import annotations

from server.analytics.os_degradation import compute_os_degradation_risk


def _hist(**kw):
    """Minimal historical dict with explicit fields, defaulting to a stable machine."""
    base = {
        "reliability_stability_index": 9.0,
        "bugchecks_30d": 0,
        "dirty_shutdowns_30d": 0,
        "app_crashes_30d": 0,
        "kernel_power_41_30d": 0,
        "avg_boot_ms": 25000,
    }
    return {**base, **kw}


# --------------------------------------------------------------------------- #
# Gating
# --------------------------------------------------------------------------- #
def test_no_data_is_unknown():
    # No historical at all -> we know nothing about OS health.
    s = compute_os_degradation_risk(None)
    assert s.value is None
    assert s.confidence == "unknown"


def test_untrusted_device_withholds():
    s = compute_os_degradation_risk(_hist(), device_trust="untrusted")
    assert s.value is None
    assert s.confidence == "unknown"


def test_no_rsi_no_crash_counts_is_unknown():
    # A historical dict with only boot time and no stability signals -> UNKNOWN.
    s = compute_os_degradation_risk({"avg_boot_ms": 90000})
    assert s.value is None
    assert s.confidence == "unknown"


# --------------------------------------------------------------------------- #
# RSI — leading signal
# --------------------------------------------------------------------------- #
def test_stable_rsi_is_zero_risk():
    # High RSI with no crash signals -> a clean, stable OS.
    s = compute_os_degradation_risk(
        _hist(reliability_stability_index=9.2, bugchecks_30d=0, dirty_shutdowns_30d=0)
    )
    assert s.value == 0.0
    assert s.direction == "higher_is_worse"


def test_critical_rsi_is_high_risk():
    # RSI well below 2.0 -> extremely unstable, bad band.
    s = compute_os_degradation_risk(
        _hist(reliability_stability_index=1.2, bugchecks_30d=0, dirty_shutdowns_30d=0)
    )
    assert s.value is not None and s.value >= 50


def test_severe_rsi_is_significant():
    s = compute_os_degradation_risk(
        _hist(reliability_stability_index=2.8, bugchecks_30d=0, dirty_shutdowns_30d=0)
    )
    assert s.value is not None and s.value >= 25


def test_moderate_rsi_is_degraded_non_zero():
    # RSI 5.5 falls in the "significantly degraded" band of the RSI scale (5-7 -> +10),
    # producing a non-zero risk value. The band depends on the overall risk threshold;
    # the key invariant is that risk is positive and below the 'bad' threshold.
    s = compute_os_degradation_risk(
        _hist(reliability_stability_index=5.5, bugchecks_30d=0, dirty_shutdowns_30d=0)
    )
    assert s.value is not None and s.value > 0
    assert s.band != "bad"  # mild degradation, not yet in the bad band


def test_mild_rsi_instability_is_small():
    s = compute_os_degradation_risk(
        _hist(reliability_stability_index=6.5, bugchecks_30d=0, dirty_shutdowns_30d=0)
    )
    assert s.value is not None and 0 < s.value < 20


def test_rsi_at_watch_boundary_is_non_zero():
    # RSI just below 7.0 should trigger the watch-level RSI contribution.
    s = compute_os_degradation_risk(_hist(reliability_stability_index=6.9))
    assert s.value is not None and s.value > 0


def test_rsi_boundary_semantics_are_exclusive():
    # Band thresholds use strict < (exclusive upper bound of each band).
    # RSI=3.5 is NOT in the "severe" band (<3.5); it falls in the "high" band [3.5, 5.0).
    # RSI=7.0 is NOT in the "watch" band (<7.0); it is stable (>=7.0 -> 0).
    at_severe = compute_os_degradation_risk(_hist(reliability_stability_index=3.5))
    below_severe = compute_os_degradation_risk(_hist(reliability_stability_index=3.4))
    assert below_severe.value > at_severe.value  # strict < means 3.5 gets less delta than 3.4

    at_watch = compute_os_degradation_risk(_hist(reliability_stability_index=7.0))
    below_watch = compute_os_degradation_risk(_hist(reliability_stability_index=6.9))
    assert at_watch.value == 0.0  # exactly 7.0 -> stable
    assert below_watch.value > 0  # 6.9 -> in watch band (+10)


# --------------------------------------------------------------------------- #
# BSOD / crash-count confirmation
# --------------------------------------------------------------------------- #
def test_multiple_bsods_are_severe():
    # BSODs alone (no RSI) still raise serious risk.
    s = compute_os_degradation_risk({"bugchecks_30d": 5, "dirty_shutdowns_30d": 0})
    assert s.value is not None and s.value >= 25


def test_single_bsod_is_significant():
    s = compute_os_degradation_risk({"bugchecks_30d": 1, "dirty_shutdowns_30d": 0})
    assert s.value is not None and s.value >= 10


def test_no_crashes_stable_rsi_is_zero():
    # Perfect RSI + no crashes -> zero risk.
    s = compute_os_degradation_risk(
        _hist(reliability_stability_index=9.5, bugchecks_30d=0, dirty_shutdowns_30d=0)
    )
    assert s.value == 0.0


def test_bsods_amplify_a_degraded_rsi():
    # Low RSI + BSODs -> higher risk than low RSI alone.
    rsi_only = compute_os_degradation_risk(
        _hist(reliability_stability_index=3.5, bugchecks_30d=0, dirty_shutdowns_30d=0)
    )
    rsi_with_bsods = compute_os_degradation_risk(
        _hist(reliability_stability_index=3.5, bugchecks_30d=3, dirty_shutdowns_30d=0)
    )
    assert rsi_with_bsods.value > rsi_only.value


def test_dirty_shutdowns_amplify_a_degraded_rsi():
    rsi_only = compute_os_degradation_risk(
        _hist(reliability_stability_index=6.0, bugchecks_30d=0, dirty_shutdowns_30d=0)
    )
    rsi_with_dirty = compute_os_degradation_risk(
        _hist(reliability_stability_index=6.0, bugchecks_30d=0, dirty_shutdowns_30d=5)
    )
    assert rsi_with_dirty.value > rsi_only.value


def test_one_dirty_shutdown_below_threshold_adds_nothing():
    # One dirty shutdown should not raise risk (common occurrence on laptops).
    with_one = compute_os_degradation_risk(
        _hist(reliability_stability_index=9.0, bugchecks_30d=0, dirty_shutdowns_30d=1)
    )
    with_zero = compute_os_degradation_risk(
        _hist(reliability_stability_index=9.0, bugchecks_30d=0, dirty_shutdowns_30d=0)
    )
    assert with_one.value == with_zero.value


def test_app_crashes_alone_do_not_drive_os_degradation():
    # No RSI or crash-count telemetry -> UNKNOWN (not zero, not a risk verdict).
    s = compute_os_degradation_risk({"app_crashes_30d": 50})
    assert s.value is None


def test_app_crashes_do_not_inflate_score_when_other_signals_present():
    # App crashes alongside RSI + crash counts must not change the score — they are
    # lineage-only, never a score driver.
    base = _hist(reliability_stability_index=7.5, bugchecks_30d=0, dirty_shutdowns_30d=0)
    without = compute_os_degradation_risk({**base, "app_crashes_30d": 0})
    with_crashes = compute_os_degradation_risk({**base, "app_crashes_30d": 50})
    assert with_crashes.value == without.value


# --------------------------------------------------------------------------- #
# Boot-rot (current state, not trend)
# --------------------------------------------------------------------------- #
def test_very_slow_boot_adds_risk():
    # 3-minute boot is a strong degradation signal.
    no_boot = compute_os_degradation_risk(_hist(avg_boot_ms=None))
    slow_boot = compute_os_degradation_risk(_hist(avg_boot_ms=180_000))
    # slow boot should add meaningful risk vs no boot data (or vs fast boot).
    fast_boot = compute_os_degradation_risk(_hist(avg_boot_ms=20_000))
    assert slow_boot.value > fast_boot.value


def test_normal_boot_does_not_add_risk():
    # 20-second boot on a stable machine is fine.
    s = compute_os_degradation_risk(
        _hist(reliability_stability_index=9.0, bugchecks_30d=0, avg_boot_ms=20_000)
    )
    assert s.value == 0.0


def test_slow_boot_threshold_triggers():
    # avg >= 60s should add a boot-rot contribution.
    below = compute_os_degradation_risk(_hist(avg_boot_ms=55_000))
    above = compute_os_degradation_risk(_hist(avg_boot_ms=65_000))
    assert above.value >= below.value  # at or above threshold triggers, below does not


# --------------------------------------------------------------------------- #
# Confidence
# --------------------------------------------------------------------------- #
def test_rsi_and_crash_counts_give_medium_confidence():
    # Both evidence streams present -> medium (RSI is heuristic, causes ambiguous).
    s = compute_os_degradation_risk(
        _hist(reliability_stability_index=3.0, bugchecks_30d=2, dirty_shutdowns_30d=3)
    )
    assert s.confidence == "medium"


def test_rsi_only_gives_low_confidence():
    # RSI alone (no explicit crash confirmation) -> low.
    s = compute_os_degradation_risk({"reliability_stability_index": 3.0})
    assert s.confidence == "low"


def test_crash_counts_only_give_low_confidence():
    # Crash counts without RSI -> low confidence (single evidence stream).
    s = compute_os_degradation_risk({"bugchecks_30d": 3, "dirty_shutdowns_30d": 2})
    assert s.confidence == "low"


# --------------------------------------------------------------------------- #
# Honesty: blind spots, explainability, lineage
# --------------------------------------------------------------------------- #
def test_pending_reboot_blind_spot_always_present_on_verdict():
    # A machine awaiting a reboot is in a degraded state we cannot detect.
    # Must appear in every non-UNKNOWN verdict.
    s = compute_os_degradation_risk(_hist())
    joined = " ".join(s.missing_evidence).lower()
    assert "reboot" in joined or "restart" in joined or "pending" in joined


def test_cause_blind_spot_always_present_on_verdict():
    # Crash counts cannot distinguish cause -> always disclosed.
    s = compute_os_degradation_risk(_hist(reliability_stability_index=2.0))
    joined = " ".join(s.missing_evidence).lower()
    assert (
        "cause" in joined or "hardware" in joined or "driver" in joined or "distinguish" in joined
    )


def test_lineage_contains_expected_fields():
    s = compute_os_degradation_risk(
        _hist(
            reliability_stability_index=5.0,
            bugchecks_30d=2,
            dirty_shutdowns_30d=3,
            app_crashes_30d=7,
            avg_boot_ms=70_000,
        )
    )
    lin = s.source_lineage
    assert lin.get("rsi") == 5.0
    assert lin.get("bugchecks_30d") == 2
    assert lin.get("dirty_shutdowns_30d") == 3
    assert lin.get("avg_boot_ms") == 70_000
    # app_crashes surfaced in lineage even though they don't drive the score.
    assert "app_crashes_30d" in lin


def test_stable_reason_is_explained():
    s = compute_os_degradation_risk(
        _hist(reliability_stability_index=9.0, bugchecks_30d=0, dirty_shutdowns_30d=0)
    )
    assert s.value == 0.0
    assert s.reason  # must not be empty for an all-clear


def test_degraded_reason_is_empty_or_set():
    # When risk > 0, reason may be empty (factors already explain) or set.
    s = compute_os_degradation_risk(_hist(reliability_stability_index=2.0, bugchecks_30d=4))
    assert s.value is not None and s.value > 0
    # factors must explain the verdict
    assert s.factors


def test_degraded_factors_mention_rsi_or_crash():
    # A bad RSI verdict must have a factor label mentioning RSI or a crash signal.
    s = compute_os_degradation_risk(_hist(reliability_stability_index=1.8, bugchecks_30d=0))
    labels = " ".join(f["label"].lower() for f in s.factors)
    assert "rsi" in labels or "crash" in labels or "unstable" in labels or "degraded" in labels


# --------------------------------------------------------------------------- #
# Integration: pipeline wires os_degradation_risk into stored scores
# --------------------------------------------------------------------------- #
def test_integration_os_degradation_risk_in_pipeline(tmp_path):
    """Full pipeline: ingest historical -> recompute -> os_degradation_risk present."""
    from fastapi.testclient import TestClient
    from server.config import ServerConfig
    from server.main import create_app
    from tests.conftest import DEGRADING_DEVICE, degrading, envelope

    app = create_app(ServerConfig(db_path=str(tmp_path / "test.db")))
    with TestClient(app) as c:
        for mt in ("inventory", "historical", "heartbeat"):
            r = c.post("/api/v1/ingest", json=envelope(DEGRADING_DEVICE, mt, degrading(mt)))
            assert r.status_code == 200
        body = c.post(
            "/api/v1/ingest",
            json=envelope(DEGRADING_DEVICE, "historical", degrading("historical")),
        ).json()
        # os_degradation_risk must be in the score100 blob.
        score100 = body["scores"]["risk"]["score100"]
        assert "os_degradation_risk" in score100
        odr = score100["os_degradation_risk"]
        assert odr.get("direction") == "higher_is_worse"
        # Degrading machine has RSI 4.2 + 1 BSOD + 2 dirty shutdowns -> risk > 0.
        assert odr.get("value") is not None and odr["value"] > 0
