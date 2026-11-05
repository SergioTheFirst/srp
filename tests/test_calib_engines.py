"""Engine calibration (spec 2026-10-09, D8 / D9 / D11).

* disk_fill: a sudden fill counts once the 4 newest readings agree, while 1-3-reading
  Windows Update staging dips stay rebound-guarded (F44); WU failures count only within
  30 days of the newest heartbeat receipt (F43).
* network_risk: 3 echoes per target -> loss is 0 / 33 / 67 / 100 %; one lost echo is
  the light tier (factor shown, axis good), two or more the heavy one.
* fleet_anomaly: a pattern needs >= 3 affected devices (the cohort includes this one).
* os_degradation: one BSOD is not counted twice (its RSI dip); RSI lowered by app
  crashes alone is capped, but only with every system counter known clean.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from server import db
from server.analytics.disk_fill import compute_disk_fill_risk
from server.analytics.fleet_anomaly import compute_fleet_anomaly_risk
from server.analytics.network_risk import compute_network_risk
from server.analytics.os_degradation import compute_os_degradation_risk

_NOW = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
_WU = "Microsoft-Windows-WindowsUpdateClient"


def _hb(free: float, age_h: float) -> dict:
    """A heartbeat row received ``age_h`` hours before ``_NOW``."""
    return {"free_space_pct": free, "received_at": (_NOW - timedelta(hours=age_h)).isoformat()}


def _weeks_at_35_then(lows: list) -> list:
    """Newest-first: ``lows`` (1 h apart, agent 0.3.8 cadence) after 42 readings at 35 %
    over 14 days."""
    recent = [_hb(v, 1.0 * i) for i, v in enumerate(lows)]
    return recent + [_hb(35.0, len(lows) + 1.0 + 7.8 * i) for i in range(42)]


def _wu(n: int, age_days: Optional[float] = None) -> list:
    """``n`` WU install failures received ``age_days`` before ``_NOW`` (None = undated)."""
    if age_days is None:
        return [{"source": _WU, "event_id": 20} for _ in range(n)]
    when = (_NOW - timedelta(days=age_days)).isoformat()
    return [{"source": _WU, "event_id": 20, "received_at": when} for _ in range(n)]


def _wu_labels(score) -> list:
    return [f["label"] for f in score.factors if "Windows Update" in f["label"]]


# --------------------------------------------------------------------------- #
# disk_fill (D8, F44 / F43)
# --------------------------------------------------------------------------- #
def test_short_low_dip_is_not_yet_a_full_disk():
    """0.3.8 (часовой цикл): 4 низких показания = ~4 ч -- провал staging WU, не «диск полон»."""
    s = compute_disk_fill_risk(_weeks_at_35_then([1.0] * 4), [])
    assert s.value != 75.0


def test_sudden_full_disk_confirmed_by_sixteen_readings():
    s = compute_disk_fill_risk(_weeks_at_35_then([1.0] * 16), [])
    assert s.value == 75.0
    assert s.band == "bad"
    # The «possible transient» note is gone once the typical level itself is low.
    assert not any("нестабильно" in m for m in s.missing_evidence)


def test_three_low_readings_still_rebound_guarded():
    s = compute_disk_fill_risk(_weeks_at_35_then([1.0] * 3), [])
    assert s.value == 0.0
    assert any("нестабильно" in m for m in s.missing_evidence)  # dip surfaced, not hidden


def test_wu_failures_older_than_30_days_ignored():
    series = [_hb(50.0, 2.0 * i) for i in range(5)]
    s = compute_disk_fill_risk(series, _wu(3, age_days=40))
    assert _wu_labels(s) == []
    assert s.value == 0.0
    assert s.source_lineage["servicing_failures"] == 0


def test_wu_failures_in_window_or_undated_still_count():
    series = [_hb(50.0, 2.0 * i) for i in range(5)]
    assert _wu_labels(compute_disk_fill_risk(series, _wu(3, age_days=10)))
    assert _wu_labels(compute_disk_fill_risk(series, _wu(3)))  # no received_at -> counts


def test_wu_window_edge_is_inclusive_at_30_days():
    series = [_hb(50.0, 2.0 * i) for i in range(5)]
    assert _wu_labels(compute_disk_fill_risk(series, _wu(3, age_days=30.0)))
    assert not _wu_labels(compute_disk_fill_risk(series, _wu(3, age_days=30.0001)))


def test_wu_window_anchors_on_newest_event_without_heartbeats():
    # No heartbeat rows: the newest event receipt is the anchor, so failures 40 days
    # older than it are stale and the engine has nothing left to judge.
    other = {"source": "disk", "event_id": 7, "received_at": _NOW.isoformat()}
    s = compute_disk_fill_risk([], [other, *_wu(3, age_days=40)])
    assert s.value is None
    assert s.confidence == "unknown"


def test_forged_heartbeat_receipt_cannot_hide_recent_wu_failures(tmp_path):
    # Security review T4: the payload is stored raw, so one heartbeat carrying its own
    # "received_at" 31 days ahead must not move the window anchor and age out real
    # failures received 2 days ago. Read through the real db layer.
    db.init_db(tmp_path / "t.db")
    for hours in (24, 20, 16, 12, 8, 4):  # stored oldest-first, as ingest does
        recv = (_NOW - timedelta(hours=hours)).isoformat()
        db.store_heartbeat("victim", recv, {"free_space_pct": 12.0}, received_at=recv)
    failures = [{"source": _WU, "event_id": 20, "ts": f"2026-10-07T0{i}:00:00Z"} for i in range(3)]
    db.store_events("victim", failures, received_at=(_NOW - timedelta(days=2)).isoformat())
    forged = {"received_at": (_NOW + timedelta(days=31)).isoformat()}
    db.store_heartbeat("victim", _NOW.isoformat(), forged, received_at=_NOW.isoformat())

    s = compute_disk_fill_risk(
        db.get_recent_heartbeats("victim", limit=200), db.get_recent_events("victim", limit=200)
    )
    assert s.source_lineage["servicing_failures"] == 3
    assert _wu_labels(s)
    assert s.value == 50.0


# --------------------------------------------------------------------------- #
# network_risk (D11: 3 echoes per target)
# --------------------------------------------------------------------------- #
def _gw_loss(loss: float) -> dict:
    gw = {
        "target_kind": "gateway",
        "target": "192.168.1.1",
        "latency_ms": 1.0,
        "loss_pct": loss,
        "samples": 3,
    }
    return {"network_adapters": [], "network_quality": [gw]}


def test_one_lost_echo_is_light_and_axis_good():
    s = compute_network_risk(_gw_loss(33.3))
    assert s.value == 8.0
    assert s.band == "good"
    assert any("потери до шлюза" in f["label"] for f in s.factors)


def test_two_lost_echoes_is_heavy():
    s = compute_network_risk(_gw_loss(66.7))
    assert s.value == 30.0
    assert s.band == "watch"


# --------------------------------------------------------------------------- #
# fleet_anomaly (D11: >= 3 affected, the cohort includes this device)
# --------------------------------------------------------------------------- #
def _cohort(**kw) -> dict:
    base = {
        "cohort_size": 2,
        "cohort_bsod_pct": 0.0,
        "cohort_kp41_pct": 0.0,
        "cohort_rsi_low_pct": 0.0,
        "site_size": 0,
        "site_kp41_pct": 0.0,
    }
    return {**base, **kw}


def test_fleet_pattern_needs_three_affected_devices():
    pair = compute_fleet_anomaly_risk(_cohort(cohort_size=2, cohort_bsod_pct=0.5))
    assert pair.value == 0.0
    assert not any("BSOD" in f["label"] for f in pair.factors)
    assert compute_fleet_anomaly_risk(_cohort(cohort_size=2, cohort_rsi_low_pct=1.0)).value == 0.0
    four = compute_fleet_anomaly_risk(_cohort(cohort_size=4, cohort_bsod_pct=0.75))
    assert four.value == 35.0


def test_site_kp41_needs_three_affected():
    assert compute_fleet_anomaly_risk(_cohort(site_size=3, site_kp41_pct=0.33)).value == 0.0
    assert compute_fleet_anomaly_risk(_cohort(site_size=5, site_kp41_pct=0.6)).value == 40.0


@pytest.mark.parametrize(
    "stats",
    [
        {"cohort_bsod_pct": 1.0},
        {"cohort_rsi_low_pct": 1.0},
        {"site_size": 2, "site_kp41_pct": 1.0},
        {"cohort_size": 5, "cohort_bsod_pct": 0.4},
    ],
)
def test_pattern_below_affected_minimum_is_not_called_normal(stats):
    # A rate at its threshold but fewer than 3 affected devices: the zero must say the
    # pattern is below that minimum, not claim normal failure rates and a stable OS.
    s = compute_fleet_anomaly_risk(_cohort(**stats))
    assert s.value == 0.0
    assert "нормальный уровень сбоев" not in s.reason
    assert "меньше 3 устройств" in s.reason


def test_quiet_cohort_keeps_normal_reason():
    s = compute_fleet_anomaly_risk(_cohort(cohort_size=5))
    assert s.value == 0.0
    assert "нормальный уровень сбоев" in s.reason


def test_junk_inputs_raise_no_new_exceptions():
    # A NaN rate is no pattern (round(NaN) would raise), and an absurd receipt time as
    # the window anchor must not overflow the 30-day arithmetic.
    for pct in ("cohort_bsod_pct", "cohort_rsi_low_pct", "site_kp41_pct"):
        s = compute_fleet_anomaly_risk(_cohort(cohort_size=5, site_size=5, **{pct: float("nan")}))
        assert s.value == 0.0
    ancient = {"source": "disk", "event_id": 7, "received_at": "0001-01-02T00:00:00+00:00"}
    assert compute_disk_fill_risk([], [ancient, *_wu(3)]).value == 28.0  # undated WU count


# --------------------------------------------------------------------------- #
# os_degradation (D11: RSI from app crashes only; one BSOD not counted twice)
# --------------------------------------------------------------------------- #
def _app_only(**kw) -> dict:
    """RSI 1.8 with app crashes as the only crashes on record."""
    base = {
        "reliability_stability_index": 1.8,
        "bugchecks_30d": 0,
        "dirty_shutdowns_30d": 0,
        "whea_errors_30d": 0,
        "app_crashes_30d": 30,
    }
    return {**base, **kw}


def test_rsi_from_app_crashes_only_is_not_os_degradation():
    s = compute_os_degradation_risk(_app_only())
    assert s.value is not None and s.value <= 10
    assert s.band == "good"
    assert any("сбоями приложений" in f["label"] for f in s.factors)


def test_rsi_cap_needs_known_clean_whea():
    assert compute_os_degradation_risk(_app_only(whea_errors_30d=None)).value >= 50


def test_rsi_cap_needs_three_app_crashes():
    # Boundary: one or two app crashes exist on almost every PC and must not cap RSI.
    two = compute_os_degradation_risk(_app_only(app_crashes_30d=2))
    assert two.value >= 50
    assert not any("сбоями приложений" in f["label"] for f in two.factors)
    three = compute_os_degradation_risk(_app_only(app_crashes_30d=3))
    assert three.value is not None and three.value <= 10
    assert any("сбоями приложений" in f["label"] for f in three.factors)


def test_rsi_with_unknown_crash_counts_keeps_rsi_led_value():
    assert compute_os_degradation_risk(_app_only(bugchecks_30d=None)).value >= 50


@pytest.mark.parametrize(
    "veto",
    [
        {"dirty_shutdowns_30d": None},
        {"dirty_shutdowns_30d": 2},
        {"whea_errors_30d": 1},
        {"app_crashes_30d": None},
    ],
)
def test_rsi_cap_vetoed_by_any_unclean_or_unknown_counter(veto):
    s = compute_os_degradation_risk(_app_only(**veto))
    assert s.value >= 50
    assert not any("сбоями приложений" in f["label"] for f in s.factors)


def test_single_bsod_rsi_dip_not_double_counted():
    hist = {
        "reliability_stability_index": 6.5,
        "bugchecks_30d": 1,
        "dirty_shutdowns_30d": 0,
        "app_crashes_30d": 0,
    }
    s = compute_os_degradation_risk(hist)
    assert s.value == 15.0
    assert sum(f["delta"] for f in s.factors) == s.value  # factors still explain the value


def test_bsods_and_low_rsi_still_add_up():
    hist = {"reliability_stability_index": 4.5, "bugchecks_30d": 2, "dirty_shutdowns_30d": 3}
    s = compute_os_degradation_risk(hist)
    assert s.value == 33.0
    assert sum(f["delta"] for f in s.factors) == s.value
