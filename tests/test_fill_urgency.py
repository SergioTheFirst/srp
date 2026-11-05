"""Spec H11 (owner decision 1): a filling system drive is urgency, not degradation.

The disk_fill trajectory is capped at 80 (x 0.85 = 68 -> R 32): ETA <= 30 days or a
breach is h2 (the horizon says how soon -- 7 days «предел уже достигнут»), never h3; a
persistent level below 8 % is an h1 floor (``fill_floor``, the free-space action, no
reaction window). The NVMe spare path and real disk evidence keep their rungs.
"""

from __future__ import annotations

import pytest
from server.analytics.disk_fill import compute_disk_fill_risk
from server.analytics.trends import _domain_risk
from server.scoring.score100 import score_to_dict

from tests.test_calib_health import FILL_ACTION, _axis, _call, _st, _trend


def _fill_axes(eta: float, **storage) -> dict:
    return {
        "storage_risk": _st(10.0, **storage),
        "trajectory_risk": _axis(_domain_risk("disk_fill", eta)),
    }


@pytest.mark.parametrize(("eta", "days"), [(20.0, 30), (60.0, 90)])
def test_a_fill_forecast_is_h2_not_h3(eta: float, days: int) -> None:
    v = _call(_fill_axes(eta), trends={"disk_fill": _trend(eta=eta)})
    assert (v.state, v.dominant, v.action) == ("h2", "disk_fill", FILL_ACTION)
    assert (v.horizon_days, v.horizon_kind) == (days, "eta")


def test_a_breach_is_h2_with_the_seven_day_horizon() -> None:
    v = _call(_fill_axes(0.0), trends={"disk_fill": _trend(eta=0.0)})
    assert (v.state, v.horizon_days) == ("h2", 7)
    assert "предел уже достигнут" in v.horizon_reason


def test_a_180_day_fill_forecast_stays_h0() -> None:
    v = _call(_fill_axes(150.0), trends={"disk_fill": _trend(eta=150.0)})
    assert (v.state, v.horizon_days, v.dominant) == ("h0", 180, "disk_fill")


def _fill_axis(level: float, *, gated: bool = False) -> dict:
    rows = [{"free_space_pct": level} for _ in range(8)]
    risk = compute_disk_fill_risk(rows, [], domain_state="unknown" if gated else None)
    return score_to_dict(risk)


@pytest.mark.parametrize(("level", "state"), [(7.9, "h1"), (8.0, "h0")])
def test_a_drive_below_8_percent_is_an_h1_floor(level: float, state: str) -> None:
    v = _call({"storage_risk": _st(10.0), "disk_fill_risk": _fill_axis(level)})
    assert v.state == state
    if state == "h1":
        assert (v.rung_source, v.dominant, v.action) == ("fill_floor", "disk_fill", FILL_ACTION)
        assert v.horizon_days is None  # urgency without a reaction window
        assert any("свободно" in (f.get("label") or "") for f in v.state_evidence)


def test_a_gated_disk_fill_sets_no_floor() -> None:
    v = _call({"storage_risk": _st(10.0), "disk_fill_risk": _fill_axis(5.0, gated=True)})
    assert v.state == "h0" and v.rung_source is None


def test_the_ladder_wins_over_the_floor() -> None:
    # a real disk h3 next to a full drive names the disk, not the fill
    axes = {
        "storage_risk": _st(40.0, damage=40.0, rloss=30.0, flags=["pending_gt10"]),
        "disk_fill_risk": _fill_axis(5.0),
    }
    v = _call(axes)
    assert (v.state, v.dominant, v.rung_source) == ("h3", "storage", "flag:pending_gt10")


# --------------------------------------------------------------------------- #
# Guards: the NVMe spare path and real disk evidence are not capped
# --------------------------------------------------------------------------- #
def test_a_running_out_nvme_spare_keeps_h3() -> None:
    assert _domain_risk("nvme_spare", 20.0) == 90.0  # the cap is the fill domain only
    axes = {"storage_risk": _st(10.0), "trajectory_risk": _axis(90.0)}
    v = _call(axes, trends={"nvme_spare": _trend(eta=20.0)})
    assert (v.state, v.dominant) == ("h3", "storage")


def test_fill_plus_pending_is_still_h3_by_the_disk() -> None:
    v = _call(_fill_axes(20.0, flags=["pending_gt10"]), trends={"disk_fill": _trend(eta=20.0)})
    assert (v.state, v.dominant) == ("h3", "storage")
