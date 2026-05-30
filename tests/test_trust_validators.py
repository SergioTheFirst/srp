from __future__ import annotations

import pytest
from server.trust.states import SemanticStatus
from server.trust.validators import (
    MATERIAL_SOURCES,
    validate_battery,
    validate_frozen_constant,
    validate_scalar_range,
    validate_source,
    validate_storage_item,
)

pytestmark = pytest.mark.unit


def test_storage_wear_above_100_is_implausible():
    status, reason = validate_storage_item({"wear_pct": 140.0}, last=None)
    assert status is SemanticStatus.IMPLAUSIBLE
    assert reason is not None


def test_storage_negative_counter_is_implausible():
    status, _ = validate_storage_item({"reallocated_sectors": -5}, last=None)
    assert status is SemanticStatus.IMPLAUSIBLE


def test_storage_clean_item_is_plausible():
    status, _ = validate_storage_item({"wear_pct": 12.0, "power_on_hours": 5200}, last=None)
    assert status is SemanticStatus.PLAUSIBLE


def test_battery_full_above_design_is_inconsistent():
    status, _ = validate_battery(
        {"present": True, "design_capacity_mwh": 50000, "full_charge_capacity_mwh": 60000}
    )
    assert status is SemanticStatus.INCONSISTENT


def test_battery_present_without_design_is_inconsistent():
    status, _ = validate_battery({"present": True, "design_capacity_mwh": None})
    assert status is SemanticStatus.INCONSISTENT


def test_scalar_out_of_range_is_implausible():
    status, _ = validate_scalar_range("free_space", 142.0, 0.0, 100.0)
    assert status is SemanticStatus.IMPLAUSIBLE


def test_scalar_in_range_is_plausible():
    status, _ = validate_scalar_range("free_space", 61.0, 0.0, 100.0)
    assert status is SemanticStatus.PLAUSIBLE


def test_thermal_constant_across_samples_is_frozen():
    # OEM fake-constant: throttle proxy never moves between samples.
    status, reason = validate_frozen_constant("throttle", value=27.0, last_value=27.0)
    assert status is SemanticStatus.FROZEN
    assert reason is not None


def test_thermal_changing_value_is_plausible():
    status, _ = validate_frozen_constant("throttle", value=83.0, last_value=97.0)
    assert status is SemanticStatus.PLAUSIBLE


def test_frozen_check_no_history_is_plausible():
    # One sample is not enough to call frozen; defer (needs >=1 prior).
    status, _ = validate_frozen_constant("throttle", value=27.0, last_value=None)
    assert status is SemanticStatus.PLAUSIBLE


def test_immaterial_source_is_unchecked():
    # CPU% / queue length never get semantic validation (materiality governor).
    status, _ = validate_source("cpu_pct", {"value": 9999.0}, last=None)
    assert status is SemanticStatus.UNCHECKED


def test_material_storage_routes_to_storage_validator():
    status, _ = validate_source("storage_reliability", {"wear_pct": 200.0}, last=None)
    assert status is SemanticStatus.IMPLAUSIBLE


def test_known_bad_firmware_is_flagged():
    status, reason = validate_source(
        "storage_reliability",
        {"model": "BadSSD X1", "firmware": "EVIL01", "wear_pct": 3.0},
        last=None,
    )
    assert status is SemanticStatus.KNOWN_BAD
    assert "EVIL01" in (reason or "")


def test_throttle_routes_to_frozen_check():
    status, _ = validate_source("throttle", {"value": 27.0}, last={"value": 27.0})
    assert status is SemanticStatus.FROZEN


def test_free_space_material_and_range_checked():
    status, _ = validate_source("free_space", {"value": 150.0}, last=None)
    assert status is SemanticStatus.IMPLAUSIBLE


def test_material_sources_set_excludes_raw_perf():
    assert "cpu_pct" not in MATERIAL_SOURCES
    assert "disk_queue" not in MATERIAL_SOURCES
    assert "storage_reliability" in MATERIAL_SOURCES


# ---------------------------------------------------------------------------
# validate_source: reliability range (0..10)
# ---------------------------------------------------------------------------


def test_reliability_above_max_is_implausible():
    status, _ = validate_source("reliability", {"value": 11.0}, None)
    assert status is SemanticStatus.IMPLAUSIBLE


# ---------------------------------------------------------------------------
# validate_source: boot_time range (0..600000)
# ---------------------------------------------------------------------------


def test_boot_time_above_max_is_implausible():
    status, _ = validate_source("boot_time", {"value": 700000.0}, None)
    assert status is SemanticStatus.IMPLAUSIBLE


# ---------------------------------------------------------------------------
# validate_source: event_counts — negative count and clean dict
# ---------------------------------------------------------------------------


def test_event_counts_negative_value_is_implausible():
    status, _ = validate_source("event_counts", {"kp41": 3, "bad": -1}, None)
    assert status is SemanticStatus.IMPLAUSIBLE


def test_event_counts_clean_dict_is_plausible():
    status, _ = validate_source("event_counts", {"kp41": 3, "ds": 0}, None)
    assert status is SemanticStatus.PLAUSIBLE


# ---------------------------------------------------------------------------
# validate_storage_item: counter-reset detection
# ---------------------------------------------------------------------------


def test_storage_counter_reset_is_inconsistent():
    status, reason = validate_storage_item({"power_on_hours": 100}, last={"power_on_hours": 5000})
    assert status is SemanticStatus.INCONSISTENT
    assert reason  # non-empty
    assert "power_on_hours" in reason or "drop" in reason.lower() or "reset" in reason.lower()


# ---------------------------------------------------------------------------
# validate_battery: additional branches
# ---------------------------------------------------------------------------


def test_battery_not_present_is_plausible():
    status, _ = validate_battery({"present": False})
    assert status is SemanticStatus.PLAUSIBLE


def test_battery_wear_above_100_is_implausible():
    status, _ = validate_battery({"present": True, "design_capacity_mwh": 50000, "wear_pct": 150})
    assert status is SemanticStatus.IMPLAUSIBLE


def test_battery_dispatch_no_design_is_inconsistent():
    status, _ = validate_source("battery", {"present": True, "design_capacity_mwh": None}, None)
    assert status is SemanticStatus.INCONSISTENT


# ---------------------------------------------------------------------------
# validate_scalar_range: non-numeric value is skipped, not a crash
# ---------------------------------------------------------------------------


def test_scalar_range_non_numeric_value_is_plausible():
    status, _ = validate_scalar_range("free_space", "garbage", 0.0, 100.0)
    assert status is SemanticStatus.PLAUSIBLE
