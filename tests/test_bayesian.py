"""Explainable Bayesian risk tests (server/scoring/bayesian.py).

The aggregation works in log-odds: posterior = sigmoid(prior + Σ evidence).
Every term is labelled, so the result is explainable by construction; these
tests pin both the numbers (healthy stays low, degrading lights up) and the
explainability contract (prior always present, factors carry weights).
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
    assert isinstance(r["top"], str)          # class name, not a dict
    assert isinstance(r["overall"], float)


def test_classes_sorted_descending_by_probability():
    r = _risk(degrading)
    probs = [c["probability"] for c in r["classes"]]
    assert probs == sorted(probs, reverse=True)
    assert r["overall"] == probs[0]
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
    assert r["overall"] < 0.10
    assert all(c["level"] == "low" for c in r["classes"])


def test_desktop_has_no_battery_class():
    """Battery risk is not applicable to a machine with no battery present."""
    r = _risk(healthy)
    names = {c["name"] for c in r["classes"]}
    assert "battery" not in names
    assert len(r["classes"]) == 4


# --------------------------------------------------------------------------- #
# Degrading machine
# --------------------------------------------------------------------------- #
def test_degrading_top_class_is_power_thermal():
    r = _risk(degrading)
    # 4x Kernel-Power 41 + WHEA + dirty shutdowns dominate.
    assert r["top"] == "power_thermal"
    assert r["overall"] > 0.5
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
# Empty input
# --------------------------------------------------------------------------- #
def test_no_data_yields_priors_only():
    r = compute_risk(None, None, None)
    assert r["overall"] < 0.10
    assert r["top"] is not None
    assert len(r["classes"]) == 4   # no battery without a battery payload
