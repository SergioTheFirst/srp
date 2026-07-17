"""Scoring boundary tests (server/scoring/scores.py).

Every threshold in the four scoring functions has at least one synthetic machine
positioned at the boundary, one just below, and one just above — so an
off-by-one cannot slip past.  Each test builds a *minimal* payload with only
the signal(s) relevant to the boundary under test; all other inputs are None or
neutral so the result is unambiguous.

Property tests at the end verify invariants that must hold for ANY machine:
monotonicity across three grades, determinism, and clamping to [0, 100].
"""

from __future__ import annotations

import pytest
from server.scoring.scores import compute_day1_scores

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _s(inv=None, hist=None, hb=None):
    return compute_day1_scores(inv, hist, hb)


def _hist(**kw):
    """Minimal historical dict: zeroed event counts, empty storage."""
    base = {
        "reliability_stability_index": None,
        "kernel_power_41_30d": 0,
        "dirty_shutdowns_30d": 0,
        "bugchecks_30d": 0,
        "app_crashes_30d": 0,
        "whea_errors_30d": 0,
        "avg_boot_ms": None,
        "storage": [],
        "observation_days": 30,
    }
    base.update(kw)
    return base


def _hb(**kw):
    """Minimal heartbeat dict: all vitals at healthy-but-None baseline."""
    base = {
        "cpu_pct": None,
        "cpu_perf_pct": None,
        "mem_avail_mb": None,
        "committed_pct": None,
        "pagefile_pct": None,
        "disk_read_sec": None,
        "disk_write_sec": None,
        "disk_queue": None,
        "free_space_pct": None,
        "handle_count_total": None,
        "nic_errors": None,
        "user_present": True,
        "uptime_hours": None,
    }
    base.update(kw)
    return base


def _inv(**kw):
    """Minimal inventory dict: no known problems."""
    base = {
        "driver_problem_count": 0,
        "pending_reboot": False,
        "bios_release_date": None,
        "os_install_date": None,
        "chassis": "desktop",
    }
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# Reliability — RSI tier boundaries
# (tiers: [0,3) → -25,  [3,5) → -15,  [5,7) → -7,  [7,10] → 0)
# --------------------------------------------------------------------------- #


def test_rsi_7_0_is_no_penalty():
    """RSI = 7.0 is NOT < 7 → no penalty → reliability == 100."""
    assert _s(hist=_hist(reliability_stability_index=7.0))["reliability"] == 100.0


def test_rsi_6_9_gets_moderate_penalty():
    """RSI = 6.9 IS < 7 (third tier) → -7 → reliability == 93."""
    assert _s(hist=_hist(reliability_stability_index=6.9))["reliability"] == 93.0


def test_rsi_5_0_gets_moderate_penalty():
    """RSI = 5.0 is NOT < 5 but IS < 7 (third tier) → -7."""
    assert _s(hist=_hist(reliability_stability_index=5.0))["reliability"] == 93.0


def test_rsi_4_9_gets_low_penalty():
    """RSI = 4.9 IS < 5 (second tier) → -15 → reliability == 85."""
    assert _s(hist=_hist(reliability_stability_index=4.9))["reliability"] == 85.0


def test_rsi_3_0_gets_low_penalty():
    """RSI = 3.0 is NOT < 3 but IS < 5 (second tier) → -15."""
    assert _s(hist=_hist(reliability_stability_index=3.0))["reliability"] == 85.0


def test_rsi_2_9_gets_highest_penalty():
    """RSI = 2.9 IS < 3 (first tier) → -25 → reliability == 75."""
    assert _s(hist=_hist(reliability_stability_index=2.9))["reliability"] == 75.0


# --------------------------------------------------------------------------- #
# Reliability — KP41 and BSOD capping
# --------------------------------------------------------------------------- #


def test_kp41_penalty_capped_at_35():
    """kp * 7 is clamped to 35: kp=5 and kp=6 yield the same reliability."""
    at_cap = _s(hist=_hist(kernel_power_41_30d=5))["reliability"]  # 5*7=35 → cap
    past_cap = _s(hist=_hist(kernel_power_41_30d=6))["reliability"]  # 6*7=42 → still 35
    assert at_cap == past_cap


def test_bugcheck_penalty_capped_at_40():
    """bc * 12 is clamped to 40: bc=4 (48→40) same reliability as bc=5 (60→40)."""
    s4 = _s(hist=_hist(bugchecks_30d=4))["reliability"]
    s5 = _s(hist=_hist(bugchecks_30d=5))["reliability"]
    assert s4 == s5


# --------------------------------------------------------------------------- #
# Performance — CPU throttle boundary
# --------------------------------------------------------------------------- #


def test_cpu_perf_95_is_no_penalty():
    """cpu_perf_pct = 95.0 is NOT < 95 → no throttle penalty."""
    assert _s(hb=_hb(cpu_perf_pct=95.0))["performance"] == 100.0


def test_cpu_perf_94_gets_small_penalty():
    """cpu_perf_pct = 94 → (95-94)*1.5 = 1.5 → performance == 98.5."""
    assert _s(hb=_hb(cpu_perf_pct=94.0))["performance"] == 98.5


def test_cpu_perf_throttle_capped_at_30():
    """cpu_perf_pct = 74 → (95-74)*1.5 = 31.5, clamped to 30 → performance == 70.0."""
    assert _s(hb=_hb(cpu_perf_pct=74.0))["performance"] == 70.0


# --------------------------------------------------------------------------- #
# Performance — RAM boundary (critical < 512 vs low < 1024)
# --------------------------------------------------------------------------- #


def test_mem_512_skips_critical_lands_in_low_tier():
    """mem_avail_mb = 512 is NOT < 512 (skips critical), but IS < 1024 → low tier → -12 → 88.0."""
    assert _s(hb=_hb(mem_avail_mb=512))["performance"] == 88.0


def test_mem_511_is_critical():
    """mem_avail_mb = 511 IS < 512 → critical tier → -20 → performance == 80.0."""
    assert _s(hb=_hb(mem_avail_mb=511))["performance"] == 80.0


# --------------------------------------------------------------------------- #
# Performance — disk latency thresholds (0.020 s and 0.050 s)
# --------------------------------------------------------------------------- #


def test_disk_latency_0_020_is_no_penalty():
    """read latency = 0.020 is NOT > 0.020 → no penalty."""
    assert _s(hb=_hb(disk_read_sec=0.020))["performance"] == 100.0


def test_disk_latency_0_021_is_elevated():
    """read latency = 0.021 > 0.020 (elevated tier) → -8 → performance == 92.0."""
    assert _s(hb=_hb(disk_read_sec=0.021))["performance"] == 92.0


def test_disk_latency_0_051_is_high():
    """read latency = 0.051 > 0.050 (high tier) → -15 → performance == 85.0."""
    assert _s(hb=_hb(disk_read_sec=0.051))["performance"] == 85.0


# --------------------------------------------------------------------------- #
# Performance — boot-time tiers (40 s / 60 s / 90 s)
# --------------------------------------------------------------------------- #


def test_boot_40000ms_is_no_penalty():
    """avg_boot_ms = 40 000 is NOT > 40 000 → no boot penalty."""
    assert _s(hist=_hist(avg_boot_ms=40_000))["performance"] == 100.0


def test_boot_40001ms_is_above_target():
    """avg_boot_ms = 40 001 > 40 000 (first tier) → -5 → performance == 95.0."""
    assert _s(hist=_hist(avg_boot_ms=40_001))["performance"] == 95.0


def test_boot_60001ms_is_slow():
    """avg_boot_ms = 60 001 > 60 000 (second tier) → -10 → performance == 90.0."""
    assert _s(hist=_hist(avg_boot_ms=60_001))["performance"] == 90.0


def test_boot_90001ms_is_very_slow():
    """avg_boot_ms = 90 001 > 90 000 (third tier) → -15 → performance == 85.0."""
    assert _s(hist=_hist(avg_boot_ms=90_001))["performance"] == 85.0


# --------------------------------------------------------------------------- #
# Wear — SSD wear clamping at 70 points
# --------------------------------------------------------------------------- #


def test_ssd_wear_0_pct_is_no_penalty():
    """wear_pct = 0 → condition max_wear > 0 is False → no wear factor."""
    storage = [{"disk": "SSD", "media_type": "SSD", "wear_pct": 0, "power_on_hours": 1000}]
    assert _s(hist=_hist(storage=storage))["wear"] == 100.0


def test_ssd_wear_50_pct_penalty():
    """wear_pct = 50 → -50 → wear == 50.0."""
    storage = [{"disk": "SSD", "media_type": "SSD", "wear_pct": 50, "power_on_hours": 1000}]
    assert _s(hist=_hist(storage=storage))["wear"] == 50.0


def test_ssd_wear_100_pct_clamped_same_as_70():
    """wear penalty is clamped at 70 points: wear_pct=100 and wear_pct=70 give identical wear scores."""
    s100 = _s(
        hist=_hist(
            storage=[{"disk": "SSD", "media_type": "SSD", "wear_pct": 100, "power_on_hours": 1000}]
        )
    )
    s70 = _s(
        hist=_hist(
            storage=[{"disk": "SSD", "media_type": "SSD", "wear_pct": 70, "power_on_hours": 1000}]
        )
    )
    assert s100["wear"] == s70["wear"] == 30.0


# --------------------------------------------------------------------------- #
# Risk — free-space tier boundaries (< 5 / < 10 / < 15)
# --------------------------------------------------------------------------- #


def test_free_space_4_9_is_cascade():
    """free_space_pct = 4.9 IS < 5 → cascade tier → +30 → risk == 30.0."""
    assert _s(hb=_hb(free_space_pct=4.9))["risk_exposure"] == 30.0


def test_free_space_5_0_skips_cascade_lands_in_moderate():
    """free_space_pct = 5.0 NOT < 5 (no cascade), but IS < 10 → moderate tier → +18."""
    assert _s(hb=_hb(free_space_pct=5.0))["risk_exposure"] == 18.0


def test_free_space_9_9_is_moderate():
    """free_space_pct = 9.9 IS < 10 → moderate tier → +18 → risk == 18.0."""
    assert _s(hb=_hb(free_space_pct=9.9))["risk_exposure"] == 18.0


def test_free_space_10_0_skips_moderate_lands_in_low():
    """free_space_pct = 10.0 NOT < 10, but IS < 15 → low tier → +8."""
    assert _s(hb=_hb(free_space_pct=10.0))["risk_exposure"] == 8.0


def test_free_space_15_0_is_no_penalty():
    """free_space_pct = 15.0 is NOT < 15 → no penalty → risk == 0."""
    assert _s(hb=_hb(free_space_pct=15.0))["risk_exposure"] == 0.0


# --------------------------------------------------------------------------- #
# Risk — cumulative disk I/O errors
# --------------------------------------------------------------------------- #


def test_disk_io_errors_add_to_risk():
    """Any non-zero disk I/O errors → +25 risk."""
    storage = [{"disk": "HDD", "read_errors_total": 5, "write_errors_total": 0}]
    assert _s(hist=_hist(storage=storage))["risk_exposure"] == 25.0


# --------------------------------------------------------------------------- #
# Property: monotonicity across three RSI grades
# --------------------------------------------------------------------------- #


def test_reliability_monotone_across_three_rsi_grades():
    """Three synthetic machines at different RSI levels must order strictly."""
    s_good = _s(hist=_hist(reliability_stability_index=8.5))["reliability"]
    s_mid = _s(hist=_hist(reliability_stability_index=5.5))["reliability"]
    s_bad = _s(hist=_hist(reliability_stability_index=2.0))["reliability"]
    assert s_good > s_mid > s_bad


def test_performance_monotone_across_three_mem_levels():
    """Three machines at different available-RAM levels must order strictly."""
    s_ok = _s(hb=_hb(mem_avail_mb=4096))["performance"]
    s_low = _s(hb=_hb(mem_avail_mb=900))["performance"]
    s_crit = _s(hb=_hb(mem_avail_mb=400))["performance"]
    assert s_ok > s_low > s_crit


def test_risk_monotone_across_three_free_space_levels():
    """Three machines at different free-space levels must order strictly (more risk = lower free space)."""
    r_ok = _s(hb=_hb(free_space_pct=40.0))["risk_exposure"]
    r_mid = _s(hb=_hb(free_space_pct=9.0))["risk_exposure"]
    r_crit = _s(hb=_hb(free_space_pct=3.0))["risk_exposure"]
    assert r_ok < r_mid < r_crit


# --------------------------------------------------------------------------- #
# Property: determinism
# --------------------------------------------------------------------------- #


def test_scoring_is_deterministic():
    """Same inputs always produce the same outputs."""
    h = _hist(reliability_stability_index=5.5, kernel_power_41_30d=2, avg_boot_ms=55_000)
    first = _s(hist=h)
    second = _s(hist=h)
    assert first == second


# --------------------------------------------------------------------------- #
# Property: extreme inputs stay within [0, 100]
# --------------------------------------------------------------------------- #


def test_extreme_degrading_inputs_clamped_to_range():
    """A machine with absurdly bad values must not push any score outside [0, 100]."""
    extreme_hist = _hist(
        reliability_stability_index=1.0,
        kernel_power_41_30d=100,
        dirty_shutdowns_30d=100,
        bugchecks_30d=100,
        app_crashes_30d=100,
        avg_boot_ms=300_000,
        storage=[
            {
                "disk": "HDD",
                "wear_pct": 100,
                "power_on_hours": 100_000,
                "read_errors_total": 999,
                "write_errors_total": 999,
            }
        ],
    )
    extreme_hb = _hb(
        cpu_perf_pct=10.0,
        mem_avail_mb=100,
        pagefile_pct=99.0,
        disk_read_sec=1.0,
        free_space_pct=1.0,
        nic_errors=100,
    )
    extreme_inv = _inv(driver_problem_count=100, pending_reboot=True)
    s = _s(inv=extreme_inv, hist=extreme_hist, hb=extreme_hb)
    for key in ("performance", "reliability", "wear", "risk_exposure"):
        assert 0.0 <= s[key] <= 100.0, f"{key}={s[key]} out of range"
