from __future__ import annotations

import pytest
from server.trust.states import SemanticStatus
from server.trust.validators import validate_battery, validate_scalar_range, validate_storage_item

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
