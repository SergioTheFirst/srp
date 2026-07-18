"""P0-6 (stoperrors.md): ``get_fleet_cohort_stats`` percentages must exclude
devices that never reported a field from the denominator, not count them as
"healthy" (0.0). A device with no ``bugchecks_30d``/``kernel_power_41_30d``/
``reliability_stability_index`` in its historical payload (old agent, or the
field genuinely never sampled) carries no evidence either way.

The plan's own suggested fix (``AND json_extract(...) IS NOT NULL`` inside the
``CASE WHEN``) turned out to be a no-op verified against a live SQLite: SQL
three-valued logic already sends a NULL comparison to ``ELSE`` on its own, so
the guard changes nothing. The real fix drops the trailing ``ELSE`` so the
``CASE`` itself returns SQL NULL for a non-reporting device, which ``AVG()``
then genuinely excludes.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def db_init(tmp_path):
    from server import db

    db.init_db(tmp_path / "t.db")
    return db


def _seed(db_init, device_id: str, model: str, site_code: str, payload: dict) -> None:
    db_init.upsert_device(
        device_id, "2026-07-01T00:00:00Z", "1.0.0", model=model, site_code=site_code
    )
    db_init.store_historical(device_id, "2026-07-01T00:00:00Z", payload)


def test_bsod_pct_excludes_non_reporting_devices(db_init):
    _seed(db_init, "d1", "OptiPlex", "site-a", {"bugchecks_30d": 3})  # reports, bad
    for i in range(3):
        # old agent -- field genuinely absent from the payload, not 0
        _seed(db_init, f"d-old-{i}", "OptiPlex", "site-a", {"avg_boot_ms": 20000})

    stats = db_init.get_fleet_cohort_stats("OptiPlex", None)

    assert stats["cohort_size"] == 4  # all 4 devices have SOME historical data
    # Buggy pre-fix result: 1/4 = 0.25 (non-reporters wrongly counted healthy).
    assert stats["cohort_bsod_pct"] == 1.0  # 1 of 1 REPORTING device is bad


def test_kp41_pct_excludes_non_reporting_devices_cohort_and_site(db_init):
    _seed(db_init, "d1", "OptiPlex", "site-a", {"kernel_power_41_30d": 5})
    for i in range(3):
        _seed(db_init, f"d-old-{i}", "OptiPlex", "site-a", {"avg_boot_ms": 20000})

    stats = db_init.get_fleet_cohort_stats("OptiPlex", "site-a")

    assert stats["cohort_kp41_pct"] == 1.0
    assert stats["site_kp41_pct"] == 1.0


def test_rsi_low_pct_excludes_non_reporting_devices(db_init):
    """The plan cites this metric as the ALREADY-correct reference pattern to
    copy -- it turned out to have the identical no-op bug, verified live."""
    _seed(db_init, "d1", "OptiPlex", "site-a", {"reliability_stability_index": 2.0})
    for i in range(3):
        _seed(db_init, f"d-old-{i}", "OptiPlex", "site-a", {"avg_boot_ms": 20000})

    stats = db_init.get_fleet_cohort_stats("OptiPlex", None)

    assert stats["cohort_rsi_low_pct"] == 1.0


def test_mixed_reporting_and_non_reporting_devices(db_init):
    """Sanity: normal mixed-fleet math still works once non-reporters are
    excluded (not just the all-or-nothing edge cases above)."""
    _seed(db_init, "d-bad", "OptiPlex", "site-a", {"bugchecks_30d": 2})
    _seed(db_init, "d-good", "OptiPlex", "site-a", {"bugchecks_30d": 0})
    _seed(db_init, "d-old", "OptiPlex", "site-a", {"avg_boot_ms": 20000})

    stats = db_init.get_fleet_cohort_stats("OptiPlex", None)

    assert stats["cohort_size"] == 3
    assert stats["cohort_bsod_pct"] == 0.5  # 1 bad of 2 REPORTING devices


def test_no_cohort_devices_is_zero_not_error(db_init):
    stats = db_init.get_fleet_cohort_stats("NoSuchModel", None)
    assert stats["cohort_size"] == 0
    assert stats["cohort_bsod_pct"] == 0.0
