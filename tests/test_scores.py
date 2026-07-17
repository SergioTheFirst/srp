"""Day-1 score tests (server/scoring/scores.py).

Key invariants:
* A clean machine scores ~100 on the three "higher is healthier" scores and 0
  on the inverted risk_exposure.
* A stressed machine drops on every score and gains risk_exposure.
* A blocked source (None) is neutral -- it must never make a machine look
  healthier than one reporting a real problem.
* Every score is explainable: the factor list names what moved it.
"""

from __future__ import annotations

import pytest
from server.scoring.scores import compute_day1_scores, device_age_years
from tests.conftest import degrading, healthy

pytestmark = pytest.mark.unit


def _scores(source):
    return compute_day1_scores(source("inventory"), source("historical"), source("heartbeat"))


# --------------------------------------------------------------------------- #
# Healthy machine
# --------------------------------------------------------------------------- #
def test_healthy_machine_scores_near_perfect():
    s = _scores(healthy)
    assert s["performance"] == 100.0
    assert s["reliability"] == 100.0
    assert s["wear"] == 100.0
    assert s["risk_exposure"] == 0.0


def test_healthy_machine_has_no_negative_factors():
    s = _scores(healthy)
    assert s["factors"]["performance"] == []
    assert s["factors"]["reliability"] == []
    assert s["factors"]["risk_exposure"] == []


# --------------------------------------------------------------------------- #
# Degrading machine
# --------------------------------------------------------------------------- #
def test_degrading_machine_scores_drop():
    s = _scores(degrading)
    assert s["performance"] < 70
    assert s["reliability"] < 40
    assert s["wear"] < 10  # 82% SSD wear + high power-on hours + old hardware
    assert s["risk_exposure"] > 30


def test_degrading_machine_explains_every_score():
    s = _scores(degrading)
    for key in ("performance", "reliability", "wear", "risk_exposure"):
        factors = s["factors"][key]
        assert factors, f"expected explanatory factors for {key}"
        assert all("label" in f and "delta" in f for f in factors)


def test_low_free_space_drives_risk_exposure():
    s = _scores(degrading)  # free_space_pct = 6 -> "cascade risk"
    labels = " ".join(f["label"] for f in s["factors"]["risk_exposure"])
    assert "free" in labels.lower()


# --------------------------------------------------------------------------- #
# Raw pre-gating layer (W0.5 Score100 owns the system verdict; see test_score100)
# --------------------------------------------------------------------------- #
def test_raw_all_none_is_pregating_baseline_not_the_verdict():
    """compute_day1_scores is the RAW heuristic: with no telemetry it returns the
    optimistic baseline. That is NOT the system's answer -- W0.5 wraps it in a
    confidence-gated Score100 that degrades all-None inputs to UNKNOWN/low
    confidence (see tests/test_score100.py)."""
    s = compute_day1_scores(None, None, None)
    assert s["performance"] == 100.0  # raw layer only; never surfaced as healthy
    assert s["risk_exposure"] == 0.0


def test_blocked_source_not_treated_as_problem():
    """A machine that blocks a heartbeat source must not score worse than one
    that reports healthy vitals."""
    blocked = compute_day1_scores(healthy("inventory"), healthy("historical"), None)
    reporting = _scores(healthy)
    assert blocked["performance"] >= reporting["performance"] - 0.001


def test_scores_are_clamped_to_unit_range():
    s = _scores(degrading)
    for key in ("performance", "reliability", "wear", "risk_exposure"):
        assert 0.0 <= s[key] <= 100.0


# --------------------------------------------------------------------------- #
# device_age_years helper
# --------------------------------------------------------------------------- #
def test_device_age_none_when_no_inventory():
    assert device_age_years(None) is None
    assert device_age_years({}) is None


def test_device_age_from_bios_date_is_positive():
    age = device_age_years({"bios_release_date": "2018-11-20"})
    assert age is not None and age > 5


def test_device_age_ignores_future_dates():
    """A clock-skewed future date yields a negative age and must be skipped."""
    assert device_age_years({"bios_release_date": "2099-01-01"}) is None
