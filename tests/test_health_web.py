"""ssd3 Ф7 T7.1 — fleet «Здоровье» dashboard (/health) + T7.3 web tests.

Two layers:

* Pure data-assembly helpers in ``server.web.dashboard`` (KPI counts, heatmap row
  build, escalation join, top-models grouping, index sparkline) -- unit-tested on
  synthetic ``get_fleet_health`` rows, no DB.
* The ``/health`` route + template + the ``db.get_fleet_health`` band extension --
  integration-tested through the ``client`` fixture (200, |tojson island, XSS
  inertness, DOMContentLoaded gate, nav item).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from server import db
from server.analytics import health
from server.web import dashboard

pytestmark = pytest.mark.integration


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _hrow(device_id: str, **over) -> dict:
    """A get_fleet_health()-shaped row (post-Ф7 band extension) with sane defaults."""
    row = {
        "device_id": device_id,
        "hostname": device_id.upper(),
        "state": "h0",
        "index": 90.0,
        "band": "good",
        "damage": 5.0,
        "resilience": 90.0,
        "observability_pct": 85.0,
        "dominant": None,
        "delta_7d": None,
        "score_ts": _iso(_now()),
        "damage_band": "good",
        "resilience_band": "good",
        "observability_band": "good",
        "axis_bands": {
            "storage": "good",
            "aging": "good",
            "os": "good",
            "battery": "good",
            "disk_fill": "good",
            "network": "good",
            "trajectory": "good",
        },
    }
    row.update(over)
    return row


# --------------------------------------------------------------------------- #
# health.action_for (the one new public accessor Ф7 adds to health.py)
# --------------------------------------------------------------------------- #
def test_action_for_maps_dominant_to_recommendation() -> None:
    assert health.action_for("storage") == health._ACTIONS["storage"]
    assert health.action_for("battery") == health._ACTIONS["battery"]


def test_action_for_none_and_unknown_fall_back_to_default() -> None:
    default = health._ACTIONS[None]
    assert health.action_for(None) == default
    assert health.action_for("no-such-mechanism") == default


# --------------------------------------------------------------------------- #
# band_class (Jinja global; Ф7 T7.2 device hero also calls it)
# --------------------------------------------------------------------------- #
def test_band_class_maps_band_vocab_to_css_class() -> None:
    assert dashboard.band_class("good") == "good"
    assert dashboard.band_class("watch") == "warn"
    assert dashboard.band_class("bad") == "bad"
    assert dashboard.band_class("unknown") == "na"
    assert dashboard.band_class(None) == "na"


# --------------------------------------------------------------------------- #
# KPI counts
# --------------------------------------------------------------------------- #
def test_kpi_counts_critical_low_obs_stale_and_worsened() -> None:
    now = _now()
    rows = [
        _hrow("d1", state="h4"),  # critical
        _hrow("d2", state="h4"),  # critical
        _hrow("d3", state="h1", observability_pct=20.0),  # low_obs
        _hrow("d4", state="h0", observability_pct=None),  # not low_obs (unknown obs)
        _hrow("d5", state="h0", score_ts=_iso(now - timedelta(days=6))),  # stale
    ]
    deltas = [{"device_id": "d3"}, {"device_id": "d5"}]
    kpi = dashboard._kpi_counts(rows, deltas, now)
    assert kpi["critical"] == 2
    assert kpi["worsened"] == 2
    assert kpi["low_obs"] == 1
    assert kpi["stale"] == 1


def test_state_distribution_buckets_none_state_into_unknown() -> None:
    rows = [_hrow("a", state="h4"), _hrow("b", state=None), _hrow("c", state="weird")]
    dist = {d["state"]: d for d in dashboard._state_distribution(rows)}
    assert dist["h4"]["count"] == 1
    assert dist["unknown"]["count"] == 2  # None + unrecognised both -> unknown
    assert dist["h4"]["label"] == health._STATE_LABELS["h4"]  # no re-translation


def test_state_distribution_colour_is_worst_real_band_not_guessed_from_state() -> None:
    """_reconcile clamps no band by state -- h1 can legitimately be band="good".
    A state->band guess table would paint the whole h1 slice "watch" regardless of
    what the real devices show; the donut must colour by the WORST band actually
    present in that bucket instead."""
    rows = [
        _hrow("h1-good", state="h1", band="good"),
        _hrow("h1-good-2", state="h1", band="good"),
        _hrow("h2-mixed-a", state="h2", band="good"),
        _hrow("h2-mixed-b", state="h2", band="bad"),  # one bad device -> whole bucket reads bad
    ]
    dist = {d["state"]: d for d in dashboard._state_distribution(rows)}
    assert dist["h1"]["band"] == "good"  # both real devices are good -> honest, not "watch"
    assert dist["h2"]["band"] == "bad"  # worst of {good, bad} is bad -> never hides the bad one


def test_state_distribution_empty_bucket_defaults_to_unknown_band() -> None:
    rows = [_hrow("a", state="h0", band="good")]
    dist = {d["state"]: d for d in dashboard._state_distribution(rows)}
    assert dist["h4"]["count"] == 0
    assert dist["h4"]["band"] == "unknown"  # no devices -> no real band to report


# --------------------------------------------------------------------------- #
# Heatmap row build
# --------------------------------------------------------------------------- #
def test_heatmap_sorts_worst_state_first_then_index_asc() -> None:
    rows = [
        _hrow("good1", state="h0", index=95.0),
        _hrow("crit_hi", state="h4", index=30.0),
        _hrow("crit_lo", state="h4", index=10.0),  # same state, lower index => first
    ]
    hm = dashboard._heatmap(rows)
    assert hm["device_ids"][:3] == ["crit_lo", "crit_hi", "good1"]


def test_heatmap_z_is_discrete_band_ordinals_in_column_order() -> None:
    row = _hrow(
        "z1",
        band="watch",
        damage_band="bad",
        resilience_band="good",
        observability_band="unknown",
        axis_bands={
            "storage": "bad",
            "aging": "good",
            "os": "watch",
            "battery": "good",
            "disk_fill": "good",
            "network": "good",
            "trajectory": "unknown",
        },
    )
    hm = dashboard._heatmap([row])
    # cols: Состояние | D | R | O | storage | aging | os | battery | disk_fill | network | trajectory
    assert hm["z"][0] == [1, 2, 0, 3, 2, 0, 1, 0, 0, 0, 3]
    assert len(hm["cols"]) == 11
    assert hm["cols"][:4] == ["Состояние", "Повреждения (D)", "Устойчивость (R)", "Видимость (O)"]


def test_heatmap_caps_at_100_rows() -> None:
    rows = [_hrow(f"d{i}", state="h2", index=float(i)) for i in range(130)]
    assert len(dashboard._heatmap(rows)["z"]) == 100


# --------------------------------------------------------------------------- #
# Escalation join
# --------------------------------------------------------------------------- #
def test_escalations_join_dominant_and_action() -> None:
    deltas = [
        {"device_id": "e1", "hostname": "ESC-1", "state": "h3", "prev_state": "h1"},
    ]
    fh_by_id = {"e1": _hrow("e1", dominant="storage")}
    out = dashboard._escalations(deltas, fh_by_id)
    assert len(out) == 1
    e = out[0]
    assert e["hostname"] == "ESC-1"
    assert e["prev_label"] == health._STATE_LABELS["h1"]
    assert e["state_label"] == health._STATE_LABELS["h3"]
    assert e["action"] == health.action_for("storage")
    assert e["dominant_label"] == health._DOMINANT_LABELS["storage"]


# --------------------------------------------------------------------------- #
# Top-risk-models grouping
# --------------------------------------------------------------------------- #
def test_risk_models_ranks_worst_mean_index_first_and_skips_none() -> None:
    rows = [
        _hrow("a", index=40.0),
        _hrow("b", index=20.0),  # model X mean = 30 (worse)
        _hrow("c", index=90.0),
        _hrow("d", index=None),  # None must not zero the mean for model Y
    ]
    model_by_id = {"a": "X", "b": "X", "c": "Y", "d": "Y"}
    models = dashboard._risk_models(rows, model_by_id)
    assert [m["model"] for m in models] == ["X", "Y"]  # 30 < 90 -> X first
    assert models[0]["mean_index"] == 30.0
    assert models[1]["mean_index"] == 90.0  # None row skipped, not averaged as 0
    assert models[1]["count"] == 1


def test_risk_models_carries_mean_drO_each_independently_skipping_none() -> None:
    """K1: the projection must never appear without the three coordinates next to
    it. Each field's mean is independent -- a device missing ONE coordinate must
    not affect another coordinate's average for the same model (a row-level "skip
    the whole device if any field is None" implementation would wrongly zero out
    every mean here, since every device below is missing a different field)."""
    rows = [
        _hrow("a", index=50.0, damage=None, resilience=80.0, observability_pct=70.0),
        _hrow("b", index=30.0, damage=60.0, resilience=None, observability_pct=90.0),
    ]
    model_by_id = {"a": "Z", "b": "Z"}
    models = dashboard._risk_models(rows, model_by_id)
    assert len(models) == 1
    z = models[0]
    assert z["model"] == "Z"
    assert z["count"] == 2  # both devices have a valid index
    assert z["mean_index"] == 40.0  # (50 + 30) / 2
    assert z["mean_damage"] == 60.0  # only b's damage counted (a's is None)
    assert z["mean_resilience"] == 80.0  # only a's resilience counted (b's is None)
    assert z["mean_observability"] == 80.0  # (70 + 90) / 2, both present


def test_risk_models_mean_drO_is_none_when_no_device_has_that_field() -> None:
    rows = [_hrow("a", index=50.0, damage=None)]
    model_by_id = {"a": "W"}
    models = dashboard._risk_models(rows, model_by_id)
    assert models[0]["mean_damage"] is None  # no device contributed -> None, not 0


# --------------------------------------------------------------------------- #
# Index sparkline (skips pre-Ф6 rows with no health key, never treats as 0)
# --------------------------------------------------------------------------- #
def test_index_sparkline_skips_missing_health_key() -> None:
    series = [  # newest-first, as get_score_series returns
        {"risk": {"health": {"index": 60.0}}},
        {"risk": {}},  # pre-Ф6 row: no health key -> gap, not 0
        {"risk": {"health": {"index": 80.0}}},
    ]
    spark = dashboard._index_sparkline(series)
    assert spark["count"] == 2  # only the two real points
    # a fabricated index=0 (from the missing row) would drag a y to the bottom edge;
    # with the row skipped, both plotted y's come from 60/80 (upper half of 0..100).
    ys = [float(p.split(",")[1]) for p in spark["points"].split()]
    assert all(y < 12 for y in ys)  # 60/80 map to the top half (viewbox height 24)


def test_worsening_selection_only_negative_delta_most_negative_first() -> None:
    rows = [
        _hrow("w1", delta_7d=-5.0),
        _hrow("w2", delta_7d=-20.0),
        _hrow("w3", delta_7d=3.0),  # improving -> excluded
        _hrow("w4", delta_7d=None),  # no delta -> excluded
    ]
    sel = dashboard._worsening_selection(rows)
    assert [r["device_id"] for r in sel] == ["w2", "w1"]


# --------------------------------------------------------------------------- #
# db.get_fleet_health band extension (end-to-end through storage)
# --------------------------------------------------------------------------- #
def _seed(device_id: str, hostname: str, risk: dict, ts: str = "") -> None:
    ts = ts or _iso(_now())
    db.touch_device(device_id, ts, "0.1.0", hostname=hostname)
    db.store_scores(device_id, ts, {"risk": risk})


def test_get_fleet_health_exposes_coord_and_axis_bands(client) -> None:
    _seed(
        "band-1",
        "BAND-1",
        {
            "health": {
                "state": "h3",
                "index": 40.0,
                "band": "bad",
                "damage": {"value": 70.0, "band": "bad"},
                "resilience": {"value": 55.0, "band": "watch"},
                "observability": {"value": 30.0, "band": "unknown"},
                "dominant": "storage",
            },
            "score100": {
                "storage_risk": {"band": "bad"},
                "software_aging_risk": {"band": "good"},
                "os_degradation_risk": {"band": "watch"},
                "battery_risk": {"band": "good"},
                "disk_fill_risk": {"band": "good"},
                "network_risk": {"band": "good"},
                "trajectory_risk": {"band": "bad"},
            },
        },
    )
    row = {r["device_id"]: r for r in db.get_fleet_health()}["band-1"]
    assert row["damage_band"] == "bad"
    assert row["resilience_band"] == "watch"
    assert row["observability_band"] == "unknown"
    assert row["axis_bands"]["storage"] == "bad"
    assert row["axis_bands"]["os"] == "watch"
    assert row["axis_bands"]["trajectory"] == "bad"
    assert row["axis_bands"]["aging"] == "good"


# --------------------------------------------------------------------------- #
# /health route + template
# --------------------------------------------------------------------------- #
def _island(body: str) -> dict:
    m = re.search(r'<script id="health-data" type="application/json">(.*?)</script>', body, re.S)
    assert m, "embedded health JSON island missing"
    return json.loads(m.group(1))


def test_health_page_renders_200_and_modules(client) -> None:
    _seed(
        "pg-1",
        "PG-1",
        {
            "health": {
                "state": "h4",
                "index": 20.0,
                "band": "bad",
                "damage": {"value": 80.0, "band": "bad"},
                "resilience": {"value": 40.0, "band": "watch"},
                "observability": {"value": 90.0, "band": "good"},
                "dominant": "storage",
                "delta_7d": -12.0,
            },
            "score100": {"storage_risk": {"band": "bad"}},
        },
    )
    r = client.get("/health")
    assert r.status_code == 200
    body = r.text
    assert "Здоровье флота" in body  # page marker (also used by smoke.py)
    assert "проекция" in body  # K1: index is labelled projection, never bare
    isl = _island(body)
    assert isl["heatmap"]["cols"][:4] == [
        "Состояние",
        "Повреждения (D)",
        "Устойчивость (R)",
        "Видимость (O)",
    ]
    assert "pg-1" in isl["heatmap"]["device_ids"]


def test_health_page_hostname_xss_is_inert(client) -> None:
    _seed(
        "xss-1",
        "<img src=x onerror=alert(1)>",
        {
            "health": {
                "state": "h3",
                "index": 30.0,
                "band": "bad",
                "damage": {"value": 70.0, "band": "bad"},
                "resilience": {"value": 50.0, "band": "watch"},
                "observability": {"value": 80.0, "band": "good"},
                "dominant": "storage",
                "delta_7d": -8.0,  # -> also appears SSR in the worsening module
            },
            "score100": {"storage_risk": {"band": "bad"}},
        },
    )
    body = client.get("/health").text
    assert "<img" not in body  # no un-escaped executable tag anywhere
    assert "<script>alert" not in body
    assert "&lt;img" in body  # escaped SSR form present -> passed through safely
    # the raw hostname still survives intact inside the JSON island (as string data)
    assert "xss-1" in _island(body)["heatmap"]["device_ids"]


def test_health_page_has_domcontentloaded_gate(client) -> None:
    _seed("dg-1", "DG-1", {"health": {"state": "h0", "index": 90.0, "band": "good"}})
    body = client.get("/health").text
    assert "DOMContentLoaded" in body  # chart init deferred (structural pin)


def test_health_page_resolves_band_colors_from_css_tokens(client) -> None:
    """Colours must come from CSS theme tokens (var(--good) etc.) resolved at chart
    init via getComputedStyle, not baked-in hex -- so all three themes track. A bare
    hex substring can legitimately appear elsewhere on the page (base.html's own
    :root token definitions, e.g. "--good: #10d97a;") so that alone is not a valid
    pin; instead pin that the discrete colorscale array is literally built FROM the
    resolved variables (goodHex/warnHex/badHex/naHex), matching device.html's own
    getComputedStyle(...).getPropertyValue(...) line-color pattern."""
    _seed("hx-1", "HX-1", {"health": {"state": "h2", "index": 55.0, "band": "watch"}})
    body = client.get("/health").text
    assert 'getPropertyValue("--good")' in body
    assert 'getPropertyValue("--warn")' in body
    assert 'getPropertyValue("--bad")' in body
    assert 'getPropertyValue("--na")' in body
    # the colorscale literal is assembled from the resolved JS variables, not
    # string literals -- this would fail if someone hardcoded hex directly here
    assert "[0, goodHex]" in body
    assert "[0.25, warnHex]" in body
    assert "[0.5, badHex]" in body
    assert "[0.75, naHex]" in body


def test_nav_has_health_link(client) -> None:
    body = client.get("/").text
    assert 'href="/health"' in body
