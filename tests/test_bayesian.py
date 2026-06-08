"""Explainable Bayesian risk tests (server/scoring/bayesian.py).

The aggregation works in log-odds: posterior = sigmoid(prior + Σ evidence).
Every term is labelled, so the result is explainable by construction; these
tests pin both the numbers (healthy stays low, degrading lights up) and the
explainability contract (prior always present, factors carry weights).

W4.3 test updates:
  - overall is now 0..100 (matches risk_exposure / W4.2 axes scale).
  - KP41 and WHEA are no longer independent risk drivers (D6); tests verify the
    demotions via dedicated cases.
  - Top class for the degrading fixture changed from power_thermal (D6 defect)
    to stability / storage (genuine signals: RSI 4.2 + SSD wear 82%).
"""

from __future__ import annotations

import pytest
from server.scoring.bayesian import compute_risk
from tests.conftest import degrading, healthy

pytestmark = pytest.mark.unit


def _risk(source):
    return compute_risk(source("inventory"), source("historical"), source("heartbeat"))


# --------------------------------------------------------------------------- #
# Shape / contract
# --------------------------------------------------------------------------- #
def test_risk_shape():
    r = _risk(healthy)
    assert set(r) == {"classes", "top", "overall"}
    assert isinstance(r["top"], str)  # class name, not a dict
    assert isinstance(r["overall"], float)
    assert 0.0 <= r["overall"] <= 100.0  # W4.3: 0..100 scale


def test_classes_sorted_descending_by_probability():
    r = _risk(degrading)
    probs = [c["probability"] for c in r["classes"]]
    assert probs == sorted(probs, reverse=True)
    # overall is the top-class probability scaled to 0..100 (W4.3 reconciliation)
    assert r["overall"] == round(probs[0] * 100, 1)
    assert r["top"] == r["classes"][0]["name"]


def test_every_class_starts_with_a_prior_factor():
    r = _risk(degrading)
    for c in r["classes"]:
        assert c["factors"], f"{c['name']} has no factors"
        assert c["factors"][0]["label"].startswith("Базовый риск")
        assert all("weight" in f for f in c["factors"])


# --------------------------------------------------------------------------- #
# Healthy machine
# --------------------------------------------------------------------------- #
def test_healthy_machine_low_risk():
    r = _risk(healthy)
    assert r["overall"] < 10.0  # W4.3: 0..100 scale; was < 0.10
    assert all(c["level"] == "low" for c in r["classes"])


def test_desktop_has_no_battery_class():
    """Battery risk is not applicable to a machine with no battery present."""
    r = _risk(healthy)
    names = {c["name"] for c in r["classes"]}
    assert "battery" not in names
    assert len(r["classes"]) == 4


# --------------------------------------------------------------------------- #
# Degrading machine — D6 fix verification
# --------------------------------------------------------------------------- #
def test_degrading_top_class_is_genuine_not_power_thermal():
    """W4.3 D6 fix: KP41 demoted to enhancer; genuine signals (RSI 4.2 + SSD
    wear 82%) now dominate over the noise-heavy power_thermal class."""
    r = _risk(degrading)
    # stability (RSI + BSOD + crashes + pending-reboot + driver error) or
    # storage (SSD wear 82% + high power-on hours) are the correct top classes.
    assert r["top"] in ("stability", "storage"), (
        f"Expected genuine risk class, got '{r['top']}' — "
        "power_thermal via KP41/WHEA alone is a D6 defect"
    )
    assert r["overall"] >= 50.0  # critical-band signal on the 0..100 scale
    assert r["classes"][0]["level"] == "critical"


def test_laptop_adds_battery_class():
    r = _risk(degrading)
    names = {c["name"] for c in r["classes"]}
    assert "battery" in names
    assert len(r["classes"]) == 5


def test_storage_risk_reflects_ssd_wear():
    r = _risk(degrading)
    storage = next(c for c in r["classes"] if c["name"] == "storage")
    labels = " ".join(f["label"] for f in storage["factors"])
    assert "Износ SSD" in labels


# --------------------------------------------------------------------------- #
# D6 enforcement: KP41 and WHEA must not drive risk independently
# --------------------------------------------------------------------------- #
def test_kp41_alone_does_not_drive_power_thermal():
    """KP41 without a confirmed anchor (throttle / dirty-shutdown) must not
    push power_thermal above the 'low' level (D6: specificity ≈ 0)."""
    hist = {"kernel_power_41_30d": 10, "dirty_shutdowns_30d": 0}
    # cpu_perf_pct = 100 → no throttle anchor; ds = 0 → no dirty-shutdown anchor
    r = compute_risk(None, hist, {"cpu_perf_pct": 100.0})
    power_thermal = next(c for c in r["classes"] if c["name"] == "power_thermal")
    assert power_thermal["level"] == "low", (
        f"KP41 alone must not exceed 'low' level; got '{power_thermal['level']}'"
    )


def test_kp41_enhances_when_anchor_present():
    """KP41 IS allowed to amplify risk when an anchor signal (throttle) exists."""
    hist_no_kp41 = {"dirty_shutdowns_30d": 0}
    hist_with_kp41 = {"kernel_power_41_30d": 8, "dirty_shutdowns_30d": 0}
    hb_throttled = {"cpu_perf_pct": 70.0}
    r_no = compute_risk(None, hist_no_kp41, hb_throttled)
    r_kp = compute_risk(None, hist_with_kp41, hb_throttled)
    pt_no = next(c for c in r_no["classes"] if c["name"] == "power_thermal")
    pt_kp = next(c for c in r_kp["classes"] if c["name"] == "power_thermal")
    assert pt_kp["probability"] > pt_no["probability"], (
        "KP41 should raise power_thermal probability when a throttle anchor is present"
    )


def test_whea_alone_does_not_drive_memory():
    """WHEA without other RAM evidence must not push memory above 'low' (D6)."""
    hist = {"whea_errors_30d": 20}
    r = compute_risk(None, hist, None)
    mem = next(c for c in r["classes"] if c["name"] == "memory")
    assert mem["level"] == "low", f"WHEA alone must not exceed 'low' level; got '{mem['level']}'"


def test_whea_not_in_power_thermal_factors():
    """WHEA must not appear as a factor in the power_thermal class (D6 removal)."""
    hist = {"whea_errors_30d": 50, "dirty_shutdowns_30d": 0, "kernel_power_41_30d": 0}
    r = compute_risk(None, hist, {"cpu_perf_pct": 100.0})
    power_thermal = next(c for c in r["classes"] if c["name"] == "power_thermal")
    labels = [f["label"] for f in power_thermal["factors"]]
    assert not any("WHEA" in label or "whea" in label for label in labels), (
        "WHEA must not appear in power_thermal factors after D6 fix"
    )


# --------------------------------------------------------------------------- #
# Domain values integration (D5 thin prioritizer)
# --------------------------------------------------------------------------- #
def test_domain_values_supplement_elevates_storage():
    """A non-zero storage_risk domain value supplements the storage class probability."""
    r_bare = compute_risk(None, None, None)
    r_supp = compute_risk(None, None, None, domain_values={"storage_risk": 80.0})
    prob_bare = next(c["probability"] for c in r_bare["classes"] if c["name"] == "storage")
    prob_supp = next(c["probability"] for c in r_supp["classes"] if c["name"] == "storage")
    assert prob_supp > prob_bare, "Domain storage_risk supplement must raise storage probability"


def test_domain_values_supplement_elevates_stability():
    """A non-zero os_degradation_risk domain value supplements the stability class."""
    r_bare = compute_risk(None, None, None)
    r_supp = compute_risk(None, None, None, domain_values={"os_degradation_risk": 70.0})
    prob_bare = next(c["probability"] for c in r_bare["classes"] if c["name"] == "stability")
    prob_supp = next(c["probability"] for c in r_supp["classes"] if c["name"] == "stability")
    assert prob_supp > prob_bare, (
        "Domain os_degradation_risk supplement must raise stability probability"
    )


def test_domain_unknown_value_none_is_neutral():
    """A None domain value (UNKNOWN from the engine) must not change the class probability."""
    r_bare = compute_risk(None, None, None)
    r_unknown = compute_risk(None, None, None, domain_values={"storage_risk": None})
    prob_bare = next(c["probability"] for c in r_bare["classes"] if c["name"] == "storage")
    prob_unknown = next(c["probability"] for c in r_unknown["classes"] if c["name"] == "storage")
    assert prob_bare == prob_unknown, "None domain value must be neutral (no effect)"


# --------------------------------------------------------------------------- #
# Empty input
# --------------------------------------------------------------------------- #
def test_no_data_yields_priors_only():
    r = compute_risk(None, None, None)
    assert r["overall"] < 10.0  # W4.3: 0..100 scale; was < 0.10
    assert r["top"] is not None
    assert len(r["classes"]) == 4  # no battery without a battery payload
