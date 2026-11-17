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


def test_storage_risk_reflects_ssd_wear():
    r = _risk(degrading)
    storage = next(c for c in r["classes"] if c["name"] == "storage")
    labels = " ".join(f["label"] for f in storage["factors"])
    assert "Износ SSD" in labels


@pytest.mark.parametrize(("sec", "stalled"), [(0.0, False), (0.5, False), (1.0, True)])
def test_storage_latency_factor_is_an_io_stall_only(sec: float, stalled: bool) -> None:
    """H9: disk_read_sec arrives in whole seconds -- the 0.9 factor is a stall of >= 1 s;
    the old 30 ms threshold fired on nothing but a stall anyway."""
    hb = {**healthy("heartbeat"), "disk_read_sec": sec}
    r = compute_risk(healthy("inventory"), healthy("historical"), hb)
    storage = next(c for c in r["classes"] if c["name"] == "storage")
    hits = [f for f in storage["factors"] if f["label"].startswith("Зависание чтения диска")]
    assert [f["weight"] for f in hits] == ([0.9] if stalled else [])


def test_storage_risk_ignores_unvalidated_disks_beyond_first():
    """P2-1: semantic validation (validate_storage_item, via
    pipeline._extract_reading) only ever checks storage[0]. bayesian._storage's
    OWN direct max()/sum() aggregation over historical.storage must not be
    influenced by a garbage/malicious reading on any disk past the first --
    it was never trust-gated, unlike disk[0]. (Scoped to this direct-aggregation
    path only: domain_values=None here means the W4.2 storage_risk ENGINE
    supplement -- server/analytics/storage.py, which legitimately aggregates
    over ALL disks for its own "worst disk wins" purpose -- is untouched by this
    fix and is a separate, still-open residual for a future option-(b) pass;
    see stoperrors.md P2-1 addendum.)"""
    hist = {
        "storage": [
            {"disk": "SSD0", "wear_pct": 0, "read_errors_uncorrected": 0, "power_on_hours": 100},
            {"disk": "HDD1", "read_errors_uncorrected": 999999},  # unvalidated: ignored
        ]
    }
    r = compute_risk(None, hist, None)
    storage = next(c for c in r["classes"] if c["name"] == "storage")
    labels = " ".join(f["label"] for f in storage["factors"])
    assert "Накопленные ошибки I/O" not in labels


# --------------------------------------------------------------------------- #
# D6 enforcement: KP41 and WHEA must not drive risk independently
# --------------------------------------------------------------------------- #
def test_kp41_alone_does_not_drive_power_thermal():
    """KP41 without a confirmed anchor (throttle under load, D10) must not
    push power_thermal above the 'low' level (D6: specificity ≈ 0)."""
    hist = {"kernel_power_41_30d": 10, "dirty_shutdowns_30d": 0}
    # cpu_perf_pct = 100 → no throttle anchor (6008 never anchors KP41 since D10)
    r = compute_risk(None, hist, {"cpu_perf_pct": 100.0})
    power_thermal = next(c for c in r["classes"] if c["name"] == "power_thermal")
    assert power_thermal["level"] == "low", (
        f"KP41 alone must not exceed 'low' level; got '{power_thermal['level']}'"
    )


def test_kp41_enhances_when_anchor_present():
    """KP41 IS allowed to amplify risk when an anchor signal (throttle) exists."""
    hist_no_kp41 = {"dirty_shutdowns_30d": 0}
    hist_with_kp41 = {"kernel_power_41_30d": 8, "dirty_shutdowns_30d": 0}
    # D10: low cpu_perf_pct is throttling only under load (idle SpeedStep is not).
    hb_throttled = {"cpu_pct": 80.0, "cpu_perf_pct": 70.0}
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
    assert len(r["classes"]) == 4  # fixed closed set: storage/power_thermal/memory/stability


# --------------------------------------------------------------------------- #
# P0-5 (stoperrors.md): a gate-failed domain must withhold its number, not
# just cosmetically label it after the fact.
# --------------------------------------------------------------------------- #
_HIGH_RISK_HIST = {
    # H10: reallocated_sectors is not sent by the agent (the dead factor is gone)
    "storage": [{"disk": "d0", "wear_pct": 90, "read_errors_uncorrected": 5}],
    "reliability_stability_index": 2.0,
    "bugchecks_30d": 8,
}


def _class(r, name):
    return next(c for c in r["classes"] if c["name"] == name)


def test_gated_class_withheld_when_domain_not_trusted():
    trusted = _class(
        compute_risk(None, _HIGH_RISK_HIST, None, domain_trust={"storage": "trusted"}), "storage"
    )
    assert trusted["probability"] is not None
    assert trusted["probability"] > 0.3  # genuinely elevated on this fixture

    untrusted = _class(
        compute_risk(None, _HIGH_RISK_HIST, None, domain_trust={"storage": "untrusted"}), "storage"
    )
    assert untrusted["probability"] is None
    assert untrusted["level"] == "unknown"
    assert untrusted["factors"] == []


def test_gated_class_ungated_when_domain_trust_not_provided():
    """domain_trust=None (old agent, no source_health) keeps pre-P0-5 behaviour."""
    r = compute_risk(None, _HIGH_RISK_HIST, None)
    assert _class(r, "storage")["probability"] is not None


def test_memory_class_never_gated_by_domain_trust():
    """memory has no trust domain in v1 (pipeline._CLASS_DOMAIN) -- passing an
    unrelated domain_trust key must not touch it."""
    r = compute_risk(None, _HIGH_RISK_HIST, None, domain_trust={"storage": "untrusted"})
    assert _class(r, "memory")["probability"] is not None


def test_withheld_class_never_becomes_top_or_crashes_sort():
    """All three gate-able classes withheld -- memory (always ungated) must
    still be found and reported as top without a None-vs-float crash."""
    r = compute_risk(
        None,
        _HIGH_RISK_HIST,
        None,
        domain_trust={"storage": "unknown", "power_thermal": "unknown", "stability": "unknown"},
    )
    assert r["top"] == "memory"
    assert r["overall"] >= 0.0  # did not crash computing top_prob


def test_storage_and_os_factor_labels_are_russian():
    """o5-E3: операторская проза дашборда — по-русски (CLAUDE.md §5). Проверяем
    ФАКТИЧЕСКИЕ факторы, доезжающие до оператора, а не текст исходника: подписи
    обоих движков (хранилище и деградация ОС) обязаны содержать кириллицу."""
    import re

    hist = {
        "reliability_stability_index": 2.0,
        "bugchecks_30d": 4,
        "storage": [{"disk": "d0", "media_type": "SSD", "wear_pct": 95, "read_errors_total": 40}],
    }
    r = compute_risk(
        None,
        hist,
        {"cpu_perf_pct": 100.0},
        domain_values={"storage_risk": 80.0, "os_degradation_risk": 70.0},
    )
    labels = [f["label"] for c in r["classes"] for f in c["factors"]]
    engine_labels = [ln for ln in labels if "/100" in ln]
    assert engine_labels, f"подписи движков не найдены среди {labels}"
    for ln in engine_labels:
        assert re.search(r"[А-Яа-я]", ln), f"английская подпись движка: {ln!r}"


def test_class_labels_name_the_failure_not_a_virtue():
    # shown with a risk level («высокий»): «Стабильность ОС/ПО — высокий» read as stable
    labels = {c["name"]: c["label"] for c in _risk(healthy)["classes"]}
    assert labels["stability"] == "Сбои ОС/ПО"
    assert not any(lab.startswith(("Стабильн", "Здоров", "Надёжн")) for lab in labels.values())
