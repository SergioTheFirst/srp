"""W4.2 battery health engine: deterministic capacity-fade verdict + honest blind spots.

The spec (cctodo W4.2): judge battery wear from the only telemetry Windows/WMI gives
us -- DesignedCapacity vs FullChargedCapacity (-> wear%) and cycle count. The hard
constraint that shapes this engine: WMI does NOT expose physical swelling, internal
resistance, or charge habits. Capacity fade is about age/usage; swelling is a separate
failure mode we cannot see ("swelling != age"). So even a pristine battery never reads
as a confident all-clear -- confidence caps at *medium* and the swelling blind spot is
always carried in missing_evidence. Gating mirrors W0.5/W4.1: untrusted withholds; no
battery (desktop) -> not applicable; present-but-no-metric -> UNKNOWN (never a confident
zero). Current-state only -- the wear *trend*/ETA lives in the W4.1 trajectory engine.
"""

from __future__ import annotations

from server.analytics.battery import compute_battery_risk


def _bat(**kw):
    base = {"present": True}
    base.update(kw)
    return base


def _hist(bat):
    return {"battery": bat}


def test_no_battery_present_is_not_applicable():
    # A desktop genuinely has no battery -- that is N/A, not a blind spot. The
    # lineage flag distinguishes it from "we couldn't see the battery".
    s = compute_battery_risk(_hist({"present": False}))
    assert s.value is None
    assert s.source_lineage.get("battery_present") is False


def test_missing_historical_is_unknown():
    s = compute_battery_risk(None)
    assert s.value is None
    assert s.confidence == "unknown"


def test_present_battery_without_any_metric_is_unknown():
    # present but no wear / capacity / cycles -> blind spot, never a confident zero.
    s = compute_battery_risk(_hist(_bat()))
    assert s.value is None
    assert s.confidence == "unknown"
    assert s.source_lineage.get("battery_present") is True


def test_healthy_battery_low_risk_capped_medium_confidence():
    s = compute_battery_risk(_hist(_bat(wear_pct=5.0, cycle_count=120)))
    assert s.value is not None and s.value < 15
    # swelling unseen -> never "high" / all-clear, even on a pristine battery.
    assert s.confidence == "medium"


def test_swelling_blind_spot_always_flagged_when_present():
    s = compute_battery_risk(_hist(_bat(wear_pct=5.0)))
    joined = " ".join(s.missing_evidence).lower()
    assert "вздути" in joined


def test_high_wear_high_risk():
    s = compute_battery_risk(_hist(_bat(wear_pct=55.0)))
    assert s.value is not None and s.value >= 40  # bad band
    assert s.direction == "higher_is_worse"


def test_service_recommended_band_for_moderate_wear():
    s = compute_battery_risk(_hist(_bat(wear_pct=22.0)))
    assert s.value is not None and 15 <= s.value < 40  # watch


def test_wear_derived_from_capacities_when_wear_pct_missing():
    # design 50000, full 30000 -> 40% wear, even with no wear_pct field present.
    s = compute_battery_risk(_hist(_bat(design_capacity_mwh=50000, full_charge_capacity_mwh=30000)))
    assert s.value is not None and s.value >= 40
    assert s.source_lineage.get("wear_source") == "derived"


def test_fcc_above_design_is_not_negative_risk():
    # fresh battery / firmware quirk: full > design -> wear clamps to 0, not negative.
    s = compute_battery_risk(_hist(_bat(design_capacity_mwh=40000, full_charge_capacity_mwh=44000)))
    assert s.value == 0.0


def test_high_cycles_add_risk_but_do_not_dominate():
    low = compute_battery_risk(_hist(_bat(wear_pct=10.0, cycle_count=50)))
    high = compute_battery_risk(_hist(_bat(wear_pct=10.0, cycle_count=1200)))
    assert high.value > low.value
    # cycles alone (healthy capacity) must not push a battery into the "bad" band.
    assert high.value < 40


def test_only_cycles_no_capacity_is_low_confidence():
    # cycles are a weak proxy without a capacity reading -> low confidence.
    s = compute_battery_risk(_hist(_bat(cycle_count=900)))
    assert s.value is not None
    assert s.confidence == "low"


def test_cycles_only_risk_stays_modest():
    s = compute_battery_risk(_hist(_bat(cycle_count=1200)))
    assert s.value is not None and s.value < 40


def test_untrusted_device_withholds():
    s = compute_battery_risk(_hist(_bat(wear_pct=55.0)), device_trust="untrusted")
    assert s.value is None
    assert s.confidence == "unknown"


def test_factors_explain_the_verdict():
    s = compute_battery_risk(_hist(_bat(wear_pct=45.0, cycle_count=1100)))
    assert s.factors  # non-empty, explainable
    labels = " ".join(f["label"].lower() for f in s.factors)
    assert "ёмкости" in labels or "износ" in labels
    assert "циклов" in labels


def test_dead_battery_zero_fcc_reads_severe():
    # FullChargedCapacity 0 (battery dead) -> 100% wear, not a healthy zero.
    s = compute_battery_risk(_hist(_bat(design_capacity_mwh=50000, full_charge_capacity_mwh=0)))
    assert s.value is not None and s.value >= 40
