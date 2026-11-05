"""Calibration D1 (T1): significant trends, honest ETA, level breach, counters.

Fixtures are noise-free unless the test is about noise; rows sit 4 h apart (the
agent cadence) unless a test says otherwise. Values are written oldest-first.
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

import pytest
from server.analytics.trends import compute_trends, trajectory_risk_score, trend_to_dict

_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _rows(values, make, *, step_h=4.0):
    return [
        {"received_at": (_BASE + timedelta(hours=i * step_h)).isoformat(), **make(v)}
        for i, v in enumerate(values)
    ]


def _free(v):
    return {"free_space_pct": v}


def _worn(v):
    return {"storage": [{"wear_pct": v}]}


def _fill(values, *, step_h=4.0):
    return compute_trends([], _rows(values, _free, step_h=step_h))["disk_fill"]


def _wear(values, *, step_h=4.0):
    return compute_trends(_rows(values, _worn, step_h=step_h), [])["storage_wear"]


def _disk(metric, values, make, *, step_h=4.0):
    return compute_trends([], [], disk_series=_rows(values, make, step_h=step_h))[metric]


def _spare(values, *, step_h=4.0):
    return _disk(
        "nvme_spare",
        values,
        lambda v: {"nvme_spare_pct": v, "nvme_spare_threshold_pct": 10.0},
        step_h=step_h,
    )


def _tail(ratios):
    rows = _rows(
        ratios, lambda r: {"disk_lat_samples": 8, "disk_read_ms_p50": 1.0, "disk_read_ms_p95": r}
    )
    return compute_trends([], rows)["disk_tail_ratio"]


def test_mk_z_known_values():
    # Local import: the symbol is new, so the rest of this file still runs RED on its own.
    from server.analytics.trends import mann_kendall_z

    def pts(values):
        return [(i * 14400.0, float(v)) for i, v in enumerate(values)]

    assert mann_kendall_z(pts([1, 2, 3, 4, 5, 6])) == pytest.approx(2.63, abs=0.01)
    assert mann_kendall_z(pts([6, 5, 4, 3, 2, 1])) == pytest.approx(-2.63, abs=0.01)
    assert mann_kendall_z(pts([0, 0, 0, 1, 1, 1])) == pytest.approx(1.75, abs=0.01)
    assert mann_kendall_z(pts([5, 5, 5, 5, 5, 5])) == 0.0


# Found once (2026-10-09): seeds whose stationary lognormal tail noise the OLD
# Theil-Sen-sign rule read as 'worsening'. Seeds where MK itself lands past 1.96
# (its designed ~2.5 % one-sided alpha) are not used -- the rate is the claim.
_NOISE_SEEDS = (3, 5, 7, 9, 12)


def test_stationary_noise_is_not_worsening():
    for seed in _NOISE_SEEDS:
        rng = random.Random(seed)
        ratios = [round(2.0 * rng.lognormvariate(0.0, 0.6), 2) for _ in range(60)]
        assert _tail(ratios).direction in ("flat", "improving"), seed


def test_gauge_needs_six_points_and_24h():
    five = _fill([50, 48, 46, 44, 42], step_h=30.0)  # 5 readings over 5 days
    assert five.direction == "insufficient"
    assert five.reason == "нужно >= 6 показаний, есть 5"
    assert five.slope_recent is None and five.accelerating is None
    short = _fill([50 - k for k in range(8)], step_h=20.0 / 7)  # 8 readings over 20 h
    assert short.direction == "insufficient"
    assert short.reason == "мало истории: нужно >= 24 ч наблюдений"


def test_slow_series_needs_72h():
    wear = list(range(10, 17))
    assert _wear(wear, step_h=8.0).direction == "insufficient"  # 2 days
    assert _wear(wear, step_h=28.0).direction == "worsening"  # 7 days


def test_boot_time_needs_72h():
    boots = _rows([30000 + 1000 * k for k in range(13)], lambda v: {"avg_boot_ms": v})
    t = compute_trends(boots, [])["boot_time"]  # +6 s/day, but only 48 h of it
    assert t.direction == "insufficient"
    assert t.reason == "мало истории: нужно >= 72 ч наблюдений"


def test_min_distinct_returns_flat_not_insufficient():
    const = _wear([40] * 11, step_h=24.0)  # 10 days of the same wear
    assert const.direction == "flat" and const.eta_days is None
    steps = _wear([7, 7, 7, 8, 8, 8], step_h=96.0 / 5)  # 4 days, one integer step
    assert steps.direction == "flat"
    assert steps.reason == "мало шагов значения: нужно >= 4 разных значений"


def test_immaterial_drift_is_flat():
    t = _fill([round(50 - 0.01 * k * 4 / 24, 6) for k in range(200)])  # -0.01 pp/day
    assert t.direction == "flat"
    assert t.eta_days is None


def test_significant_but_immaterial_relative_drift_is_flat():
    # Tail ratio 2.00 -> 2.10 over 33 days: Mann-Kendall is far past 1.96, but 0.09
    # per 30 days is under 10 % of the current level (floor_rel).
    assert _tail([2.0 + 0.1 * k / 199 for k in range(200)]).direction == "flat"


def test_eta_requires_72h_span():
    short = _fill([60 - 2 * k * 4 / 24 for k in range(13)])  # -2 pp/day over 48 h
    assert short.direction == "worsening"
    assert short.eta_days is None
    long = _fill([60 - 2 * k * 4 / 24 for k in range(25)])  # over 96 h
    assert long.eta_days is not None


def test_disk_fill_eta_targets_five_percent():
    t = _fill([50 - 2 * k * 4 / 24 for k in range(61)])  # 50 -> 30 over 10 days
    assert t.direction == "worsening"
    # H7: the level is the median of the last 4 readings (31, 30.67, 30.33, 30 -> 30.5)
    assert t.eta_days == pytest.approx((30.5 - 5) / 2)  # not 30 / 2


def _runaway(k):
    # Часовой цикл агента 0.3.8: 800 показаний (~33 дня), k последних падают на 4 pp/сут.
    return [40.0] * (800 - k) + [40.0 - 4 * (i + 1) / 24 for i in range(k)]


def test_runaway_after_long_flat_window():
    t = _fill(_runaway(72), step_h=1.0)
    assert t.direction == "worsening"
    assert t.eta_days is not None and t.eta_days <= 7
    early = _fill(_runaway(48), step_h=1.0)
    assert early.eta_days is not None and early.eta_days <= 30


def test_runaway_needs_three_twelve_hour_blocks():
    """0.3.8: блоки runaway -- по ~12 ч (12 часовых показаний), а не по 3 показания:
    обрыв короче трёх блоков ещё не «runaway»."""
    t = _fill(_runaway(30), step_h=1.0)
    assert t.eta_days is None or t.eta_days > 7


def test_finished_copy_step_has_no_short_eta():
    t = _fill([40.0] * 190 + [32.0] * 10)
    assert t.eta_days is None or t.eta_days > 30


# Found once (2026-10-09) with design/final_eta_rules3.py: for this seed the draft
# 'steepest of window and recent half' rule (V0) reaches ETA 17.5 d at day 4.5 on
# S9-like noise, while the true ETA there is ~110 d.
_S9_SEED = 4


def test_recent_half_needs_its_own_significance():
    rng = random.Random(_S9_SEED)
    free = [round(16.5 - 0.1 * (k * 4 / 24) + rng.gauss(0, 0.6), 1) for k in range(45 * 6 + 1)]
    rows = _rows(free, _free)
    for k in range(0, len(rows), 3):
        t = compute_trends([], rows[max(0, k - 199) : k + 1])["disk_fill"]
        assert t.eta_days is None or t.eta_days > 30, (k, t.eta_days)


def test_level_breach_sets_eta_zero():
    t = _fill([35.0] * 760 + [1.0] * 16, step_h=1.0)  # ~16 ч ниже порога при часовом цикле
    assert t.direction == "worsening"
    assert t.eta_days == 0.0
    assert "порог" in t.reason


def test_three_low_readings_are_not_a_breach():
    t = _fill([35.0] * 190 + [1.0] * 3)
    assert t.eta_days != 0.0


def test_level_breach_needs_no_history():
    # 16 hourly readings over 15 h: below the 24 h gate, yet already crossed.
    fill = _fill([1.2 - 0.02 * i for i in range(16)], step_h=1.0)
    assert (fill.direction, fill.eta_days) == ("worsening", 0.0)
    spare = _spare([8.0] * 16, step_h=1.0)  # a fresh disk already under its spare threshold
    assert (spare.direction, spare.eta_days) == ("worsening", 0.0)


def test_level_breach_carries_no_slope():
    # A system disk that sat at the floor for weeks and now slowly recovers, still
    # <= 5 % free: the level breaches, yet the window slope points the improving way.
    # db.get_cohort_slope_verdict read that slope as "degrading faster than most".
    t = _fill([3.0 + 0.01 * k for k in range(150)])  # 3.00 -> 4.49 % over 25 days
    assert (t.direction, t.eta_days, t.slope_per_day) == ("worsening", 0.0, None)
    label = trajectory_risk_score({"disk_fill": t}).factors[0]["label"]
    assert label == "disk_fill → 0д до предела"


def test_non_finite_readings_are_missing_not_a_breach():
    t = _fill([40.0] * 30 + [math.nan, -math.inf] * 2)
    assert (t.direction, t.eta_days, t.current) == ("flat", None, 40.0)


def test_non_finite_current_does_not_flatten_a_worsening_gauge():
    t = _tail([2.0 + 0.05 * k for k in range(40)] + [math.nan])  # 2.00 -> 3.95, then garbage
    assert t.direction == "worsening"
    assert t.current == pytest.approx(3.95)


def test_cleanup_restarts_disk_fill_trend():
    fill = [40.0 - 4 * (i + 1) / 6 for i in range(53)]  # runaway 40 -> ~4.7
    t = _fill([40.0] * 30 + fill + [45.0] * 3)  # +40 pp cleanup, then 2 more readings
    assert t.direction == "insufficient"
    assert t.eta_days is None


def test_spare_step_collapse_gets_short_eta():
    t = _spare([100.0] * 190 + [30.0] * 3)
    assert t.direction == "worsening"
    assert t.eta_days is not None and t.eta_days <= 7


def test_spare_single_unit_drop_is_far():
    t = _spare([100.0] * 190 + [99.0] * 10)
    assert t.direction == "worsening"
    assert t.eta_days is not None and t.eta_days > 180


def test_old_spare_collapse_takes_window_rate():
    # The collapse is 200 h (> 7 days) old: the Theil-Sen median and the 7-day rate
    # are both 0, so only the window-average rate can project the ETA.
    t = _spare([100.0] * 150 + [30.0] * 50)
    window_rate = 70.0 / (199 * 4 / 24)  # pp/day over the 33-day window
    assert t.direction == "worsening"
    assert t.eta_days == pytest.approx((30.0 - 10.0) / window_rate)


def test_spare_below_threshold_breach():
    t = _spare([100.0] * 20 + [8.0] * 4)
    assert t.direction == "worsening"
    assert t.eta_days == 0.0


def test_wear_past_100_is_not_imminent():
    trends = compute_trends(_rows(range(96, 105), _worn, step_h=24.0), [])  # 8 days
    t = trends["storage_wear"]
    assert t.direction == "worsening"
    assert t.eta_days is None
    s = trajectory_risk_score(trends)
    assert s.value is not None and s.value <= 35
    assert any("ресурс записи исчерпан" in f["label"] for f in s.factors)
    assert not any(m.startswith("storage_wear") for m in s.missing_evidence)


def test_wear_eta_capped_at_180_day_band():
    trends = compute_trends(_rows(range(70, 81), _worn, step_h=24.0), [])  # +1/day to 80
    assert trends["storage_wear"].eta_days == pytest.approx(20.0)
    assert trajectory_risk_score(trends).value == 35.0


def test_trajectory_is_the_riskiest_domain_not_the_soonest_eta():
    wear = _rows(range(70, 81), _worn, step_h=24.0)  # ETA 20 d, capped at 35
    fill = _rows([65 - k * 4 / 24 for k in range(61)], _free)  # -1 pp/day to 55: ETA 50 d
    trends = compute_trends(wear, fill)
    assert trends["storage_wear"].eta_days == pytest.approx(20.0)
    # H7: the level is the median of the last 4 readings (55.5 .. 55.0 -> 55.25)
    assert trends["disk_fill"].eta_days == pytest.approx(50.25)
    assert trajectory_risk_score(trends).value == 60.0  # max risk, not the soonest ETA's 35


def test_trajectory_high_confidence_needs_seven_days():
    def confidence(n):
        return trajectory_risk_score(compute_trends([], _rows([60.0] * n, _free))).confidence

    assert confidence(19) == "medium"  # 3 days of a stable disk
    assert confidence(61) == "high"  # 10 days


def test_counter_toggle_is_flat():
    t = _disk("smart_pending", [k % 2 for k in range(24)], lambda v: {"smart_attrs": {"197": v}})
    assert t.direction == "flat"
    assert t.accelerating is not True


def test_counter_step_is_worsening_without_fake_acceleration():
    t = _disk("smart_realloc", [8] * 180 + [9] * 12, lambda v: {"smart_attrs": {"5": v}})
    assert t.direction == "worsening"
    assert t.accelerating is not True


def test_trend_dict_has_span_days():
    t = _wear(list(range(10, 16)), step_h=24.0)  # 6 readings over 5 days
    assert trend_to_dict(t)["span_days"] == pytest.approx(5.0)


# --------------------------------------------------------------------------- #
# H7 (P0-3): one stray reading must not move the fill forecast to "reached"
# --------------------------------------------------------------------------- #
def _slow(values_tail):
    # 60 readings sliding 7.5 -> 6.3 % (ETA ~45 days), then the given tail
    return [7.5 - 0.02 * k for k in range(60)] + list(values_tail)


def test_one_low_reading_does_not_make_the_fill_eta_zero():
    t = _fill(_slow([4.0]))
    assert t.direction == "worsening"
    assert t.eta_days is not None and t.eta_days > 7  # the median of the last 4 is ~6.3 %


def test_three_of_four_readings_at_the_limit_reach_it():
    t = _fill(_slow([4.9, 4.8, 4.7, 5.5]))  # the last one bounced back, the level did not
    assert t.eta_days == 0.0
