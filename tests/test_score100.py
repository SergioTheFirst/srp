"""W0.5 confidence-gated Score100 envelope.

The governing rule (telemetry-trust-contract): UNKNOWN over false confidence.
Missing or untrusted telemetry must never read as a confident healthy score.
These tests pin that the day-1 numbers are wrapped in a Score100 that withholds
the value (or drops confidence) exactly when the evidence does not support it.
"""

from __future__ import annotations

import pytest
from server.scoring.score100 import (
    band_for_health_score,
    band_for_risk_score,
    compute_day1_score100,
    compute_observability_score,
    legacy_value,
)
from server.scoring.scores import compute_day1_scores
from tests.conftest import envelope, healthy

_ALL_DOMAINS = ("storage", "disk_fill", "os_stability", "boot", "thermal")


def _trust(states=None, sources=None):
    """Build a stored-trust dict (db.get_trust shape); domains default trusted."""
    states = states or {}
    domains = {}
    for d in _ALL_DOMAINS:
        st = states.get(d, "trusted")
        domains[d] = {
            "state": st,
            "weight": 1.0 if st == "trusted" else 0.0,
            "contributing": [],
            "dropped": [],
            "reason": "",
        }
    return {"domains": domains, "sources": sources or {}}


def _day1(source):
    return compute_day1_scores(source("inventory"), source("historical"), source("heartbeat"))


# --------------------------------------------------------------------------- #
# Band helpers (unit)
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_band_helpers():
    assert band_for_health_score(None) == "unknown"
    assert band_for_health_score(90) == "good"
    assert band_for_health_score(50) == "watch"
    assert band_for_health_score(10) == "bad"
    assert band_for_risk_score(None) == "unknown"
    assert band_for_risk_score(5) == "good"
    assert band_for_risk_score(25) == "watch"
    assert band_for_risk_score(80) == "bad"


# --------------------------------------------------------------------------- #
# 1. all-None inputs are no longer high-confidence healthy
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_all_none_inputs_not_confident_healthy():
    day1 = compute_day1_scores(None, None, None)  # raw heuristic is optimistic
    s = compute_day1_score100(day1, None, None, None, trust=None)
    for axis in ("performance", "reliability", "wear", "risk_exposure"):
        assert s[axis].confidence != "high"
        # never a confident "good": either withheld or clearly low/unknown
        assert not (s[axis].band == "good" and s[axis].confidence == "high")


# --------------------------------------------------------------------------- #
# 2. healthy trusted telemetry -> numeric, high confidence
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_healthy_trusted_is_numeric_high_confidence():
    day1 = _day1(healthy)
    s = compute_day1_score100(
        day1,
        healthy("inventory"),
        healthy("historical"),
        healthy("heartbeat"),
        trust=_trust(),
    )
    assert s["performance"].value == 100.0
    assert s["performance"].confidence == "high"
    assert s["performance"].band == "good"
    assert s["wear"].confidence == "high"
    assert s["risk_exposure"].value == 0.0
    assert s["risk_exposure"].direction == "higher_is_worse"
    assert s["observability"].confidence == "high"
    assert s["observability"].value >= 80


# --------------------------------------------------------------------------- #
# 3. blocked storage -> wear unknown / low confidence + missing_evidence
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_blocked_storage_makes_wear_uncertain():
    day1 = _day1(healthy)
    s = compute_day1_score100(
        day1,
        healthy("inventory"),
        healthy("historical"),
        healthy("heartbeat"),
        trust=_trust({"storage": "unknown"}),
    )
    w = s["wear"]
    assert w.confidence in ("low", "unknown")
    assert not (w.band == "good" and w.confidence == "high")
    assert any("storage" in m for m in w.missing_evidence)


# --------------------------------------------------------------------------- #
# 4. untrusted identity -> all day-1 values withheld
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_untrusted_identity_withholds_all_values():
    day1 = _day1(healthy)
    s = compute_day1_score100(
        day1,
        healthy("inventory"),
        healthy("historical"),
        healthy("heartbeat"),
        trust=_trust(),
        device_trust="untrusted",
    )
    for axis in ("performance", "reliability", "wear", "risk_exposure"):
        assert s[axis].value is None
        assert s[axis].band == "unknown"
        assert s[axis].confidence == "unknown"
        assert "идентичность устройства не подтверждена" in s[axis].missing_evidence


# --------------------------------------------------------------------------- #
# 5. old agent (no source_health) -> accepted but low/unknown confidence
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_old_agent_no_source_health_is_low_confidence():
    day1 = _day1(healthy)
    s = compute_day1_score100(
        day1, healthy("inventory"), healthy("historical"), healthy("heartbeat"), trust=None
    )
    for axis in ("performance", "reliability", "wear", "risk_exposure"):
        assert s[axis].confidence in ("low", "unknown")
        assert "source_health отсутствует" in s[axis].missing_evidence
    # telemetry was received, so legacy numbers remain available for the dashboard
    assert legacy_value(s["performance"]) is not None


# --------------------------------------------------------------------------- #
# 6. observability falls with UNKNOWN domains / regressed sources / no health
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_observability_reflects_coverage():
    presence_ok = {"has_source_health": True, "device_trust": "ok", "clock_drift": False}
    full = compute_observability_score(_trust(), presence_ok)
    degraded = compute_observability_score(
        _trust(
            {"storage": "unknown", "thermal": "unknown"},
            sources={"storage_reliability": {"regressed": True, "state": "unavailable"}},
        ),
        presence_ok,
    )
    blind = compute_observability_score(
        None, {"has_source_health": False, "device_trust": "ok", "clock_drift": False}
    )
    assert full.value > degraded.value > blind.value
    assert full.confidence == "high"
    assert "source_health отсутствует" in blind.missing_evidence
    assert any("деградировал" in m for m in degraded.missing_evidence)


@pytest.mark.unit
def test_observability_untrusted_is_bad_not_withheld_low_confidence():
    """Untrusted identity: observability is PENALISED to a bad value (it must
    surface that the telemetry can't be trusted, not hide it as None) but at low
    confidence -- we are not confident about anything from an untrusted device."""
    s = compute_observability_score(
        _trust(),  # all domains trusted, but the identity is not
        {"has_source_health": True, "device_trust": "untrusted", "clock_drift": False},
    )
    assert s.value is not None and s.value <= 10.0
    assert s.band == "bad"
    assert s.confidence == "low"
    assert "идентичность устройства не подтверждена" in s.missing_evidence


@pytest.mark.unit
def test_observability_no_applicable_domains_is_unknown():
    """Zero applicable domains -> no coverage ratio -> UNKNOWN, not a 0/bad reading."""
    all_na = dict.fromkeys(_ALL_DOMAINS, "not_applicable")
    s = compute_observability_score(
        _trust(all_na), {"has_source_health": True, "device_trust": "ok", "clock_drift": False}
    )
    assert s.value is None
    assert s.band == "unknown"
    assert s.confidence == "unknown"


# --------------------------------------------------------------------------- #
# 7. legacy numeric fields survive end-to-end + Score100 map is exposed
# --------------------------------------------------------------------------- #
@pytest.mark.integration
def test_pipeline_keeps_legacy_fields_and_adds_score100(client):
    sh = {
        "storage_reliability": {"status": "ok", "collected_at": "2026-06-03T00:00:00+00:00"},
        "reliability": {"status": "ok", "collected_at": "2026-06-03T00:00:00+00:00"},
        "boot_time": {"status": "ok", "collected_at": "2026-06-03T00:00:00+00:00"},
    }
    env = envelope("w05", "historical", healthy("historical"))
    env["source_health"] = sh
    assert client.post("/api/v1/ingest", json=env).status_code == 200

    sc = client.get("/api/v1/devices/w05").json()["scores"]
    # legacy numeric columns still present (dashboard/API must not explode)
    for key in ("performance", "reliability", "wear", "risk_exposure"):
        assert key in sc
    # Score100 envelope rides in the risk blob with all five axes
    s100 = sc["risk"]["score100"]
    assert set(s100) >= {"performance", "reliability", "wear", "risk_exposure", "observability"}
    assert s100["performance"]["direction"] == "higher_is_better"
    assert s100["risk_exposure"]["direction"] == "higher_is_worse"
    assert "confidence" in s100["wear"] and "band" in s100["wear"]


# --------------------------------------------------------------------------- #
# 8. presence_ok gates must include every source their own axis reads (P2-12)
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_risk_exposure_presence_ok_includes_inventory():
    """_risk_exposure() (scores.py) reads inv.pending_reboot/driver_problem_count
    directly, so a device that ONLY reports inventory still produces a real,
    non-null risk_exposure signal -- presence_ok must not withhold it just
    because historical/heartbeat are absent (mirrors wear's already-correct
    gate, which includes both of its sources)."""
    inventory = {"pending_reboot": True, "driver_problem_count": 3}
    day1 = compute_day1_scores(inventory, None, None)
    assert day1["factors"]["risk_exposure"]  # real signal was computed
    risk = compute_day1_score100(day1, inventory, None, None, trust=None)["risk_exposure"]
    assert risk.value == day1["risk_exposure"]
    assert risk.band == band_for_risk_score(day1["risk_exposure"])
    assert risk.band != "unknown"


@pytest.mark.unit
def test_performance_presence_ok_includes_historical():
    """_performance() (scores.py) reads avg_boot_ms straight from historical, so
    a device that ONLY reports historical (no live heartbeat) still produces a
    real performance signal -- presence_ok must not withhold it just because
    heartbeat is absent."""
    historical = {"avg_boot_ms": 65000}
    day1 = compute_day1_scores(None, historical, None)
    assert day1["factors"]["performance"]  # real signal was computed
    perf = compute_day1_score100(day1, None, historical, None, trust=None)["performance"]
    assert perf.value == day1["performance"]
    assert perf.band == band_for_health_score(day1["performance"])
    assert perf.band != "unknown"
