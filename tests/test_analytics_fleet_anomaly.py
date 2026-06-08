"""W4.2 fleet-anomaly engine: coordinated fleet-event detection.

Detects two fleet-wide patterns that would otherwise generate false individual
hardware alerts:

  * Bad OS patch / driver rollout: many devices with the same model suddenly show
    elevated BSODs or RSI degradation — not simultaneous hardware failures, but a
    fleet-wide software event.
  * Site-wide KP41 cluster: many devices at the same site show elevated
    kernel_power_41_30d — characteristic of a building power blip or UPS trip, not
    individual PSU failures.

Score semantics: higher = more evidence of a fleet-wide event affecting this device.
Cohort key: model (from devices table). Site key: site_code.
Minimum cohort for any verdict: 2 devices. Smaller → UNKNOWN.
Confidence caps at medium (statistical power is low at small fleet sizes).
"""

from __future__ import annotations

from server.analytics.fleet_anomaly import compute_fleet_anomaly_risk


def _stats(**kw) -> dict:
    """Minimal cohort stats dict, defaulting to a clean single-device fleet."""
    base = {
        "cohort_size": 1,
        "cohort_bsod_pct": 0.0,
        "cohort_kp41_pct": 0.0,
        "cohort_rsi_low_pct": 0.0,
        "site_size": 1,
        "site_kp41_pct": 0.0,
    }
    return {**base, **kw}


# --------------------------------------------------------------------------- #
# Gating
# --------------------------------------------------------------------------- #
def test_no_cohort_data_is_unknown():
    # None cohort_stats means no fleet data at all.
    s = compute_fleet_anomaly_risk(None)
    assert s.value is None
    assert s.confidence == "unknown"


def test_single_device_cohort_is_unknown():
    # Only 1 device with this model — can't compare to any cohort.
    s = compute_fleet_anomaly_risk(_stats(cohort_size=1))
    assert s.value is None
    assert s.confidence == "unknown"


def test_untrusted_device_withholds():
    s = compute_fleet_anomaly_risk(_stats(cohort_size=5), device_trust="untrusted")
    assert s.value is None
    assert s.confidence == "unknown"


def test_two_device_cohort_returns_verdict():
    # 2 devices is the minimum — should produce a verdict (even if low confidence).
    s = compute_fleet_anomaly_risk(_stats(cohort_size=2))
    assert s.value is not None


# --------------------------------------------------------------------------- #
# Site-wide KP41 cluster (building power, not PC hardware)
# --------------------------------------------------------------------------- #
def test_strong_site_kp41_cluster_raises_risk():
    # ≥50% of site devices with elevated KP41 → building power event.
    s = compute_fleet_anomaly_risk(_stats(cohort_size=4, site_size=6, site_kp41_pct=0.67))
    assert s.value is not None and s.value >= 30


def test_moderate_site_kp41_cluster_raises_risk():
    # 30-50% site cluster → suspicious, adds moderate risk.
    s = compute_fleet_anomaly_risk(_stats(cohort_size=4, site_size=6, site_kp41_pct=0.33))
    assert s.value is not None and s.value > 0


def test_site_kp41_cluster_requires_min_site_size():
    # High pct on a tiny site (1 device) is meaningless — no cluster signal.
    s_small = compute_fleet_anomaly_risk(_stats(cohort_size=2, site_size=1, site_kp41_pct=1.0))
    s_large = compute_fleet_anomaly_risk(_stats(cohort_size=2, site_size=6, site_kp41_pct=1.0))
    assert s_large.value > s_small.value


def test_low_site_kp41_pct_adds_nothing():
    # < 30% of site devices with KP41 → below _KP41_MOD threshold, adds nothing.
    s = compute_fleet_anomaly_risk(_stats(cohort_size=4, site_size=8, site_kp41_pct=0.12))
    assert s.value == 0.0


# --------------------------------------------------------------------------- #
# Cohort BSOD rate (bad patch / driver rollout)
# --------------------------------------------------------------------------- #
def test_high_cohort_bsod_rate_raises_risk():
    # 50%+ of cohort with BSODs → fleet event (bad driver/patch), not hardware.
    s = compute_fleet_anomaly_risk(_stats(cohort_size=6, cohort_bsod_pct=0.5))
    assert s.value is not None and s.value >= 25


def test_moderate_cohort_bsod_rate_raises_risk():
    # 30%+ adds moderate signal.
    s = compute_fleet_anomaly_risk(_stats(cohort_size=6, cohort_bsod_pct=0.33))
    assert s.value is not None and s.value > 0


def test_low_cohort_bsod_rate_adds_nothing():
    # < 10% is within expected random variation — no fleet signal.
    s = compute_fleet_anomaly_risk(_stats(cohort_size=10, cohort_bsod_pct=0.05))
    assert s.value == 0.0


def test_cohort_bsod_score_same_confidence_differs_by_size():
    # The score delta is identical for both sizes (no per-device scaling — the pattern
    # is binary: either enough devices are affected or not).  What differs is confidence:
    # a large cohort gets "medium", a small one gets "low".
    small = compute_fleet_anomaly_risk(_stats(cohort_size=2, cohort_bsod_pct=1.0))
    large = compute_fleet_anomaly_risk(_stats(cohort_size=10, cohort_bsod_pct=1.0))
    assert small.value == large.value  # same signal strength regardless of size
    assert large.confidence == "medium"  # 10 >= _COHORT_MEDIUM (5)
    assert small.confidence == "low"  # 2 < _COHORT_MEDIUM


# --------------------------------------------------------------------------- #
# Cohort RSI degradation (fleet-wide OS degradation)
# --------------------------------------------------------------------------- #
def test_high_cohort_rsi_low_rate_raises_risk():
    # ≥40% of cohort with RSI < 5.0 → fleet-wide OS degradation.
    s = compute_fleet_anomaly_risk(_stats(cohort_size=8, cohort_rsi_low_pct=0.5))
    assert s.value is not None and s.value >= 15


def test_low_cohort_rsi_low_rate_adds_nothing():
    s = compute_fleet_anomaly_risk(_stats(cohort_size=8, cohort_rsi_low_pct=0.1))
    assert s.value == 0.0


# --------------------------------------------------------------------------- #
# Signal combination
# --------------------------------------------------------------------------- #
def test_multiple_signals_combine():
    # Multiple fleet patterns together produce a higher score than each alone.
    site_only = compute_fleet_anomaly_risk(_stats(cohort_size=6, site_size=6, site_kp41_pct=0.6))
    bsod_only = compute_fleet_anomaly_risk(_stats(cohort_size=6, cohort_bsod_pct=0.5))
    combined = compute_fleet_anomaly_risk(
        _stats(cohort_size=6, site_size=6, site_kp41_pct=0.6, cohort_bsod_pct=0.5)
    )
    assert combined.value > site_only.value
    assert combined.value > bsod_only.value


def test_clean_fleet_is_zero_risk():
    # Large cohort with no anomalies → zero fleet risk.
    s = compute_fleet_anomaly_risk(
        _stats(
            cohort_size=10,
            cohort_bsod_pct=0.0,
            cohort_kp41_pct=0.0,
            cohort_rsi_low_pct=0.0,
            site_size=10,
            site_kp41_pct=0.0,
        )
    )
    assert s.value == 0.0
    assert s.direction == "higher_is_worse"


# --------------------------------------------------------------------------- #
# Confidence
# --------------------------------------------------------------------------- #
def test_large_cohort_gives_medium_confidence():
    # 5+ devices in cohort → medium confidence.
    s = compute_fleet_anomaly_risk(_stats(cohort_size=5, site_size=5, cohort_bsod_pct=0.6))
    assert s.confidence == "medium"


def test_small_cohort_gives_low_confidence():
    # 2-4 devices → low.
    s = compute_fleet_anomaly_risk(_stats(cohort_size=3, cohort_bsod_pct=0.5))
    assert s.confidence == "low"


# --------------------------------------------------------------------------- #
# Honesty: lineage and explainability
# --------------------------------------------------------------------------- #
def test_stable_fleet_reason_is_explained():
    s = compute_fleet_anomaly_risk(_stats(cohort_size=8))
    assert s.value == 0.0
    assert s.reason  # non-empty explanation for all-clear


def test_fleet_event_has_factors():
    # When a fleet pattern is detected, factors must explain which pattern.
    s = compute_fleet_anomaly_risk(_stats(cohort_size=6, site_size=8, site_kp41_pct=0.6))
    assert s.factors


def test_factor_labels_mention_pattern():
    s = compute_fleet_anomaly_risk(_stats(cohort_size=6, cohort_bsod_pct=0.5))
    labels = " ".join(f["label"].lower() for f in s.factors)
    assert "bsod" in labels or "crash" in labels or "patch" in labels or "driver" in labels


def test_missing_evidence_always_present():
    # os_build not used for cohort key is always disclosed.
    s = compute_fleet_anomaly_risk(_stats(cohort_size=5))
    joined = " ".join(s.missing_evidence).lower()
    assert "build" in joined or "firmware" in joined or "cohort" in joined


def test_lineage_contains_cohort_stats():
    s = compute_fleet_anomaly_risk(
        _stats(cohort_size=8, cohort_bsod_pct=0.25, site_size=10, site_kp41_pct=0.4)
    )
    lin = s.source_lineage
    assert lin.get("cohort_size") == 8
    assert lin.get("site_size") == 10


# --------------------------------------------------------------------------- #
# Integration: pipeline wires fleet_anomaly_risk into stored scores
# --------------------------------------------------------------------------- #
def test_integration_fleet_anomaly_risk_in_pipeline(tmp_path):
    """Full pipeline: ingest → recompute → fleet_anomaly_risk present in score100."""
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
        score100 = body["scores"]["risk"]["score100"]
        assert "fleet_anomaly_risk" in score100
        far = score100["fleet_anomaly_risk"]
        assert far.get("direction") == "higher_is_worse"
        # Single device → cohort_size=1 → UNKNOWN.
        assert far.get("value") is None
