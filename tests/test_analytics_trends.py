"""W4.1 deterministic trend engine: robust slopes + ETA + trajectory_risk axis.

Pins the governing rules (cctodo §4.1 / telemetry-trust-contract):
  * slope is **robust** (Theil-Sen) -- one counter reset must not flip the trend;
  * x-axis is **server time** (received_at), never the client clock;
  * **UNKNOWN over false confidence** -- too few points, or a stable/improving
    metric, yields *no* ETA rather than a fabricated one;
  * the aggregate trajectory_risk reuses the W0.5 envelope gating (untrusted ->
    withheld; no usable depletion trend -> UNKNOWN).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from server import db
from server.analytics.diagnostics import compute_diagnostics
from server.analytics.trends import (
    build_trend,
    compute_trends,
    eta_days_to_threshold,
    theil_sen_slope,
    trajectory_risk_score,
)
from server.pipeline import recompute_scores

_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _at(day: float) -> str:
    return (_BASE + timedelta(days=day)).isoformat()


def _hist(day: float, *, wear=None, boot=None, battery_wear=None) -> dict:
    row: dict = {"received_at": _at(day), "ts": _at(day)}
    if wear is not None:
        row["storage"] = [{"wear_pct": wear}]
    if boot is not None:
        row["avg_boot_ms"] = boot
    if battery_wear is not None:
        row["battery"] = {"present": True, "wear_pct": battery_wear}
    return row


def _hb(day: float, *, free=None, perf=None) -> dict:
    row: dict = {"received_at": _at(day), "ts": _at(day)}
    if free is not None:
        row["free_space_pct"] = free
    if perf is not None:
        row["cpu_perf_pct"] = perf
    return row


# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #
def test_theil_sen_slope_per_day():
    # +10 units over 10 days -> 1.0/day.
    pts = [(0.0, 10.0), (5 * 86400.0, 15.0), (10 * 86400.0, 20.0)]
    assert theil_sen_slope(pts) == 1.0


def test_theil_sen_robust_to_single_outlier():
    # A clean +1/day ramp with one wild spike; the median slope ignores it.
    pts = [(d * 86400.0, float(d)) for d in range(6)]
    pts[3] = (3 * 86400.0, 99.0)  # counter-reset / glitch
    assert abs(theil_sen_slope(pts) - 1.0) < 0.5


def test_theil_sen_no_time_separation_returns_none():
    assert theil_sen_slope([(0.0, 1.0), (0.0, 2.0)]) is None


def test_eta_only_when_heading_toward_threshold():
    assert eta_days_to_threshold(20.0, 1.0, 100.0) == 80.0  # rising to a cap
    assert eta_days_to_threshold(20.0, -1.0, 100.0) is None  # falling away
    assert eta_days_to_threshold(20.0, 0.0, 100.0) is None  # flat


# --------------------------------------------------------------------------- #
# build_trend
# --------------------------------------------------------------------------- #
def test_insufficient_history_is_unknown():
    series = [_hist(0, wear=10), _hist(5, wear=12)]  # only 2 points
    t = build_trend(
        series,
        "storage_wear",
        lambda r: r["storage"][0]["wear_pct"],
        worsening_sign=1,
        threshold=100.0,
    )
    assert t.direction == "insufficient"
    assert t.eta_days is None


def test_worsening_wear_projects_eta_and_date():
    series = [_hist(d, wear=10 + d) for d in (0, 5, 10)]  # 10->20, +1/day
    trends = compute_trends(series, [])
    t = trends["storage_wear"]
    assert t.direction == "worsening"
    assert t.eta_days == 80.0  # (100-20)/1
    assert t.target_date is not None
    assert t.current == 20.0


def test_already_at_threshold_is_imminent_not_dropped():
    # A drive that has reached 100% wear while still rising must read ETA 0 (the
    # worst real case), not "no crossing" -> zero risk.
    series = [_hist(d, wear=96 + 2 * d) for d in (0, 2, 4)]  # 96, 100, 104
    t = compute_trends(series, [])["storage_wear"]
    assert t.direction == "worsening"
    assert t.eta_days == 0.0
    s = trajectory_risk_score(compute_trends(series, []))
    assert s.value is not None and s.value >= 90  # imminent -> top trajectory risk


def test_drive_replacement_anchors_to_new_part():
    # Old drive ramps 80->88, then is swapped for a fresh one ramping 2->8. The
    # slope must reflect the NEW drive, not average across the replacement cliff.
    series = [
        _hist(0, wear=80),
        _hist(5, wear=84),
        _hist(10, wear=88),
        _hist(15, wear=2),
        _hist(20, wear=5),
        _hist(25, wear=8),
    ]
    t = compute_trends(series, [])["storage_wear"]
    assert t.current == 8.0  # latest = new drive
    assert 0 < t.slope_per_day < 2.0  # ~0.6/day on the new drive, not a wild cross-cliff value
    assert t.n_points == 3  # anchored to the post-swap segment


def test_improving_metric_has_no_eta():
    series = [_hist(d, wear=30 - d) for d in (0, 5, 10)]  # wear falling
    t = compute_trends(series, [])["storage_wear"]
    assert t.direction == "improving"
    assert t.eta_days is None


def test_flat_metric_is_flat_not_worsening():
    series = [_hist(d, wear=40) for d in (0, 5, 10)]
    t = compute_trends(series, [])["storage_wear"]
    assert t.direction == "flat"
    assert t.eta_days is None


def test_disk_fill_falling_free_space_worsens_toward_zero():
    hbs = [_hb(d, free=50 - 2 * d) for d in (0, 5, 10)]  # 50 -> 30, -2/day
    t = compute_trends([], hbs)["disk_fill"]
    assert t.direction == "worsening"
    assert t.eta_days == 15.0  # 30 / 2
    assert t.current == 30.0


def test_boot_trend_direction_only_no_threshold():
    series = [_hist(d, boot=20000 + 1000 * d) for d in (0, 5, 10)]
    t = compute_trends(series, [])["boot_time"]
    assert t.direction == "worsening"
    assert t.eta_days is None  # unbounded metric -> no crossing


def test_received_at_used_over_client_ts():
    # ts implies a 1/day ramp; received_at implies a 0.5/day ramp. Server time wins.
    series = []
    for d in (0, 5, 10):
        r = {"received_at": _at(d), "ts": _at(2 * d), "storage": [{"wear_pct": 10 + d}]}
        series.append(r)
    t = compute_trends(series, [])["storage_wear"]
    assert abs(t.slope_per_day - 1.0) < 1e-9  # 1 wear-unit per received_at day


# --------------------------------------------------------------------------- #
# trajectory_risk aggregate (Score100 gating)
# --------------------------------------------------------------------------- #
def test_untrusted_identity_withholds_trajectory():
    series = [_hist(d, wear=10 + d) for d in (0, 5, 10)]
    s = trajectory_risk_score(compute_trends(series, []), device_trust="untrusted")
    assert s.value is None
    assert s.band == "unknown"
    assert "идентификация не подтверждена" in s.missing_evidence


def test_no_depletion_history_is_unknown_not_zero():
    # boot trend exists but is not a depletion domain -> still UNKNOWN.
    series = [_hist(d, boot=20000 + 1000 * d) for d in (0, 5, 10)]
    s = trajectory_risk_score(compute_trends(series, []))
    assert s.value is None
    assert s.band == "unknown"


def test_imminent_disk_fill_drives_high_trajectory_risk():
    hbs = [_hb(d, free=20 - 1 * d) for d in (0, 5, 10)]  # 20 -> 10, -1/day, ~10d left
    s = trajectory_risk_score(compute_trends([], hbs))
    assert s.value is not None and s.value >= 60
    assert s.band == "bad"
    assert s.factors  # soonest ETA explained


def test_stable_trends_score_zero_good():
    series = [_hist(d, wear=40) for d in (0, 5, 10)]
    hbs = [_hb(d, free=60) for d in (0, 5, 10)]
    s = trajectory_risk_score(compute_trends(series, hbs))
    assert s.value == 0.0
    assert s.band == "good"


def test_confidence_scales_with_sample_count():
    few = [_hist(d, wear=10 + d) for d in (0, 5, 10)]  # 3 points
    many = [_hist(d, wear=10 + d) for d in range(0, 12, 2)]  # 6 points
    assert trajectory_risk_score(compute_trends(few, [])).confidence == "medium"
    assert trajectory_risk_score(compute_trends(many, [])).confidence == "high"


# --------------------------------------------------------------------------- #
# Pipeline + read-side integration
# --------------------------------------------------------------------------- #
def _seed_series(device_id: str) -> None:
    db.upsert_device(device_id, _at(0), "0.1.0", hostname="WEAR-01")
    for d in (0, 4, 8, 12):
        db.store_historical(
            device_id,
            _at(d),
            {"storage": [{"wear_pct": 60 + d}], "avg_boot_ms": 20000},
            received_at=_at(d),
        )


def test_recompute_persists_trajectory(client):
    # ``client`` fixture wires the module-global DB to a temp file via app lifespan.
    _seed_series("wear-eta-01")
    scores = recompute_scores("wear-eta-01")
    assert scores is not None
    traj = scores["risk"]["score100"]["trajectory_risk"]
    assert traj["direction"] == "higher_is_worse"
    assert traj["value"] is not None  # wear rising -> a real ETA
    assert "storage_wear" in scores["risk"]["trajectory"]
    assert scores["risk"]["trajectory"]["storage_wear"]["direction"] == "worsening"


def test_diagnostics_endpoint_returns_trajectory(client):
    _seed_series("wear-eta-02")
    recompute_scores("wear-eta-02")
    result = compute_diagnostics("wear-eta-02")
    assert result is not None
    assert result["trajectory_risk"]["value"] is not None
    assert result["trends"]["storage_wear"]["eta_days"] is not None


def test_diagnostics_unknown_device_is_none(client):
    assert compute_diagnostics("nope-404") is None


def test_diagnostics_http_404_and_200(seeded_client):
    assert seeded_client.get("/api/v1/diagnostics/does-not-exist").status_code == 404
    from tests.conftest import HEALTHY_DEVICE

    resp = seeded_client.get(f"/api/v1/diagnostics/{HEALTHY_DEVICE}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["device_id"] == HEALTHY_DEVICE
    assert "trajectory_risk" in body and "trends" in body


def test_gateway_latency_trend_direction_only():
    """Rising gateway latency reads worsening (no ETA - no failure boundary);
    full-loss probes are excluded; trajectory_risk value is NOT driven by it."""
    from server.analytics.trends import compute_trends, trajectory_risk_score

    def row(day, lat, loss=0.0):
        return {
            "received_at": f"2026-06-{day:02d}T00:00:00+00:00",
            "network_quality": [
                {
                    "target_kind": "gateway",
                    "target": "192.168.1.1",
                    "latency_ms": lat,
                    "loss_pct": loss,
                    "samples": 3,
                }
            ],
        }

    def hb(day, free=50.0):
        return {"received_at": f"2026-06-{day:02d}T00:00:00+00:00", "free_space_pct": free}

    series = [row(9, 80.0), row(7, 40.0), row(5, 20.0), row(3, 5.0), row(1, 1.0)]
    hb_series = [hb(d) for d in (9, 7, 5, 3, 1)]  # stable disk -> trajectory has data
    trends = compute_trends(series, hb_series)
    t = trends["gateway_latency"]
    assert t.direction == "worsening"
    assert t.eta_days is None and t.slope_per_day > 0
    # full-loss probe carries no usable latency
    assert compute_trends([row(1, None, loss=100.0)], [])["gateway_latency"].n_points == 0
    # direction-only metrics never add trajectory risk: stable depletion -> 0.0
    assert trajectory_risk_score(trends).value == 0.0
    # ...and in isolation (no depletion data at all) they cannot fabricate a
    # value either: trajectory stays honestly UNKNOWN (review MEDIUM #5)
    isolated = compute_trends(series, [])
    assert isolated["gateway_latency"].direction == "worsening"
    assert trajectory_risk_score(isolated).value is None


def test_disk_tail_ratio_extractor_needs_4_samples_and_nonzero_p50():
    from server.analytics.trends import _disk_tail_ratio

    assert (
        _disk_tail_ratio({"disk_lat_samples": 3, "disk_read_ms_p50": 1.0, "disk_read_ms_p95": 5.0})
        is None
    )
    assert (
        _disk_tail_ratio(
            {"disk_lat_samples": None, "disk_read_ms_p50": 1.0, "disk_read_ms_p95": 5.0}
        )
        is None
    )
    assert (
        _disk_tail_ratio({"disk_lat_samples": 5, "disk_read_ms_p50": 0, "disk_read_ms_p95": 5.0})
        is None
    )
    assert (
        _disk_tail_ratio({"disk_lat_samples": 5, "disk_read_ms_p50": None, "disk_read_ms_p95": 5.0})
        is None
    )
    assert (
        _disk_tail_ratio({"disk_lat_samples": 5, "disk_read_ms_p50": 2.0, "disk_read_ms_p95": 8.0})
        == 4.0
    )


def test_disk_tail_ratio_trend_worsening_stays_out_of_trajectory_risk():
    """Growing tail at a calm mean is direction-only R-evidence (Ф4 for Ф6) --
    it must never join the depletion domains / drive trajectory_risk here."""

    def hb(day, ratio):
        return {
            "received_at": f"2026-06-{day:02d}T00:00:00+00:00",
            "disk_lat_samples": 8,
            "disk_read_ms_p50": 1.0,
            "disk_read_ms_p95": ratio,  # p50=1.0 -> ratio equals p95 directly
        }

    hb_series = [hb(9, 8.0), hb(7, 6.0), hb(5, 4.0), hb(3, 2.5), hb(1, 2.0)]
    trends = compute_trends([], hb_series)
    t = trends["disk_tail_ratio"]
    assert t.direction == "worsening"
    assert t.eta_days is None  # direction-only: no failure boundary
    # No depletion-domain telemetry at all (no storage/battery/disk_fill data
    # in this fixture) -- trajectory_risk stays honestly UNKNOWN, not fabricated.
    assert trajectory_risk_score(trends).value is None


def test_disk_tail_ratio_noisy_single_reading_excluded():
    """<4 valid PS-side samples this heartbeat -> excluded from the series."""

    def hb(day, samples, ratio):
        return {
            "received_at": f"2026-06-{day:02d}T00:00:00+00:00",
            "disk_lat_samples": samples,
            "disk_read_ms_p50": 1.0,
            "disk_read_ms_p95": ratio,
        }

    series = [hb(5, 2, 9.0), hb(3, 8, 3.0), hb(1, 8, 2.0)]  # first row noisy -> dropped
    t = compute_trends([], series)["disk_tail_ratio"]
    assert t.n_points == 2
