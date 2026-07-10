"""ssd3 Ф6 T6.2 — wiring tests: compute_health -> pipeline / db / api.

Task 1 (tests/test_health.py) exhaustively covers ``compute_health`` itself; this
file covers only the wiring T6.2 adds:

* pipeline persistence of the verdict, ``delta_7d``, and the ratchet round-trip
  through ``store_scores`` / ``get_score_series`` (the one behaviour that can only
  be verified at the wiring level);
* the two fleet readers (``get_fleet_health`` / ``get_fleet_health_deltas``),
  including escalation hysteresis + the K5 unknown exclusion;
* the ``GET /devices/{id}/health`` endpoint and its read-side staleness overlay.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from server import db, pipeline
from tests.conftest import HEALTHY_DEVICE, envelope, healthy

pytestmark = pytest.mark.integration

_RANK = {"h0": 0, "h1": 1, "h2": 2, "h3": 3, "h4": 4}


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ingest(c, device_id: str, source=healthy) -> None:
    for msg in ("inventory", "historical", "heartbeat", "events"):
        r = c.post("/api/v1/ingest", json=envelope(device_id, msg, source(msg)))
        assert r.status_code == 200, r.text


def _latest_health(device_id: str) -> Optional[dict]:
    rows = db.get_score_series(device_id, limit=1)
    return rows[0]["risk"].get("health") if rows else None


def _blob(**over) -> dict:
    """A plausible stored health blob (asdict(HealthVerdict) + delta_7d)."""
    blob = {
        "state": "h1",
        "state_label": "",
        "index": 80.0,
        "band": "good",
        "confidence": "high",
        "dominant": None,
        "delta_7d": None,
        "damage": {"value": 10.0, "band": "good"},
        "resilience": {"value": 90.0, "band": "good"},
        "observability": {"value": 88.0, "band": "good"},
        "blind_spots": [],
        "missing_evidence": [],
        "factors": [{"label": "x", "delta": 0}],
    }
    blob.update(over)
    return blob


def _seed_device(device_id: str, hostname: str, health: dict, ts: Optional[str] = None) -> None:
    ts = ts or _iso(_now())
    db.touch_device(device_id, ts, "0.1.0", hostname=hostname)
    db.store_scores(device_id, ts, {"risk": {"health": health}})


def _seed_series(device_id: str, hostname: str, seq: list) -> None:
    """seq = list of (state, ts) or (state, ts, band), OLDEST first (=> id order)."""
    db.touch_device(device_id, seq[-1][1], "0.1.0", hostname=hostname)
    for state, ts, *rest in seq:
        band = rest[0] if rest else "watch"
        db.store_scores(device_id, ts, {"risk": {"health": {"state": state, "band": band}}})


# --------------------------------------------------------------------------- #
# Part A — pipeline: verdict persisted, delta_7d, ratchet round-trip
# --------------------------------------------------------------------------- #
def test_recompute_populates_health_block(client) -> None:
    _ingest(client, HEALTHY_DEVICE)
    h = _latest_health(HEALTHY_DEVICE)
    assert h is not None
    for key in (
        "state",
        "index",
        "band",
        "damage",
        "resilience",
        "observability",
        "dominant",
        "delta_7d",
    ):
        assert key in h, f"health blob missing {key!r}"
    assert h["state"] in _RANK  # a real derived state, not garbage
    assert isinstance(h["damage"], dict) and "value" in h["damage"]


def test_delta_7d_none_on_first_recompute(client) -> None:
    _ingest(client, HEALTHY_DEVICE)
    assert _latest_health(HEALTHY_DEVICE)["delta_7d"] is None


def test_delta_7d_numeric_when_old_row_exists(client) -> None:
    _ingest(client, HEALTHY_DEVICE)
    old_ts = _iso(_now() - timedelta(days=8))
    db.store_scores(HEALTHY_DEVICE, old_ts, {"risk": {"health": {"index": 42.0}}})
    pipeline.recompute_scores(HEALTHY_DEVICE)
    h = _latest_health(HEALTHY_DEVICE)
    assert h["index"] is not None
    assert h["delta_7d"] == round(h["index"] - 42.0, 1)


def test_ratchet_persists_across_recompute(client) -> None:
    # A fresh healthy device scores near h0 (no prev_health to hold it back).
    _ingest(client, HEALTHY_DEVICE)
    fresh = _latest_health(HEALTHY_DEVICE)["state"]
    assert _RANK[fresh] <= 2  # healthy really is near the top
    # Seed a bad prev_health as the newest stored row, then recompute ONCE on the
    # same (still healthy) data. Naively that jumps to ~h0, but the ratchet — fed
    # prev_health round-tripped through store_scores/get_score_series — forbids a
    # >1-step improvement in a single pass.
    db.store_scores(HEALTHY_DEVICE, _iso(_now()), {"risk": {"health": {"state": "h4"}}})
    pipeline.recompute_scores(HEALTHY_DEVICE)
    after = _latest_health(HEALTHY_DEVICE)["state"]
    assert _RANK[after] >= _RANK["h4"] - 1  # capped: hold (h4) or one step (h3)
    assert _RANK[fresh] < _RANK[after]  # ratchet demonstrably held it above naive


def test_ratchet_disk_replacement_permits_full_reset(client) -> None:
    """Wiring-level counterpart to Task 1's test_ratchet_disk_replacement_permits_reset
    (which could only fixture-inject prev_health["worst_disk"] directly). Proves the
    real round-trip: pipeline persists worst_disk (final-review fix) so a genuinely
    different disk between two recomputes lets the ratchet's disk-swap branch fire for
    real, resetting past the one-step cap -- not just holding or advancing one rung."""
    _ingest(client, HEALTHY_DEVICE)
    natural = _latest_health(HEALTHY_DEVICE)
    assert natural["state"] == "h0"
    assert natural["worst_disk"] == "Samsung SSD 980"  # this fixture's disk model
    # Seed a synthetic prev row: worse state (h3) with a DIFFERENT worst_disk, as if
    # the physical drive was swapped since. No positive ratchet evidence exists at
    # this history depth (no reboot_restores flag, no mature flat-counter trends), so
    # WITHOUT the disk-swap branch this would hold at h3 or advance one step to h2.
    db.store_scores(
        HEALTHY_DEVICE,
        _iso(_now()),
        {"risk": {"health": {"state": "h3", "worst_disk": "OLD-FAILED-DRIVE"}}},
    )
    pipeline.recompute_scores(HEALTHY_DEVICE)
    after = _latest_health(HEALTHY_DEVICE)
    assert after["state"] == "h0"  # full reset to the naive state, not held/capped
    assert _RANK[after["state"]] < _RANK["h2"]  # strictly more than one step from h3


# --------------------------------------------------------------------------- #
# Part B — db.get_fleet_health / get_fleet_health_deltas
# --------------------------------------------------------------------------- #
def test_get_fleet_health_shape(client) -> None:
    _seed_device(
        "fh-a",
        "HOST-A",
        _blob(
            state="h0",
            index=95.0,
            band="good",
            dominant=None,
            delta_7d=2.0,
            damage={"value": 5.0},
            resilience={"value": 92.0},
            observability={"value": 80.0},
        ),
    )
    _seed_device(
        "fh-b",
        "HOST-B",
        _blob(
            state="h3",
            index=40.0,
            band="bad",
            dominant="storage",
            delta_7d=-15.0,
            damage={"value": 70.0},
            resilience={"value": 60.0},
            observability={"value": 55.0},
        ),
    )
    rows = {r["device_id"]: r for r in db.get_fleet_health()}
    assert {"fh-a", "fh-b"} <= set(rows)
    a = rows["fh-a"]
    assert a["hostname"] == "HOST-A"
    assert a["state"] == "h0" and a["index"] == 95.0 and a["band"] == "good"
    assert a["damage"] == 5.0 and a["resilience"] == 92.0
    assert a["observability_pct"] == 80.0
    assert a["dominant"] is None and a["delta_7d"] == 2.0
    assert a["score_ts"]
    b = rows["fh-b"]
    assert b["state"] == "h3" and b["dominant"] == "storage" and b["delta_7d"] == -15.0


def test_get_fleet_health_latest_row_wins(client) -> None:
    db.touch_device("fh-c", _iso(_now()), "0.1.0", hostname="HOST-C")
    db.store_scores(
        "fh-c", _iso(_now() - timedelta(hours=8)), {"risk": {"health": _blob(state="h3")}}
    )
    db.store_scores("fh-c", _iso(_now()), {"risk": {"health": _blob(state="h0")}})
    rows = {r["device_id"]: r for r in db.get_fleet_health()}
    assert rows["fh-c"]["state"] == "h0"  # newest row (highest id) wins


def test_escalation_fires(client) -> None:
    now = _now()
    _seed_series(
        "esc-1",
        "ESC-1",
        [
            ("h1", _iso(now - timedelta(days=8))),
            ("h1", _iso(now - timedelta(days=6))),
            ("h1", _iso(now - timedelta(days=2))),
            ("h3", _iso(now - timedelta(hours=6))),  # rn=2 (prev recent)
            ("h3", _iso(now - timedelta(hours=1))),  # rn=1 (current)
        ],
    )
    out = {r["device_id"]: r for r in db.get_fleet_health_deltas()}
    assert "esc-1" in out
    assert out["esc-1"]["state"] == "h3" and out["esc-1"]["prev_state"] == "h1"
    assert out["esc-1"]["hostname"] == "ESC-1"


def test_flipflop_does_not_escalate(client) -> None:
    now = _now()
    _seed_series(
        "ff-1",
        "FF-1",
        [
            ("h1", _iso(now - timedelta(days=8))),
            ("h3", _iso(now - timedelta(days=1))),
            ("h1", _iso(now - timedelta(hours=6))),  # rn=2: recovered
            ("h3", _iso(now - timedelta(hours=1))),  # rn=1: one-off spike
        ],
    )
    assert "ff-1" not in {r["device_id"] for r in db.get_fleet_health_deltas()}


def test_band_only_change_does_not_escalate(client) -> None:
    now = _now()
    _seed_series(
        "bo-1",
        "BO-1",
        [
            ("h1", _iso(now - timedelta(days=8)), "watch"),
            ("h1", _iso(now - timedelta(hours=6)), "bad"),
            ("h1", _iso(now - timedelta(hours=1)), "bad"),  # same state, worse band
        ],
    )
    assert "bo-1" not in {r["device_id"] for r in db.get_fleet_health_deltas()}


def test_unknown_transition_does_not_escalate(client) -> None:
    now = _now()
    # Came back from blind: 7d-ago state was "unknown" (off the ordinal scale, K5).
    _seed_series(
        "uk-1",
        "UK-1",
        [
            ("unknown", _iso(now - timedelta(days=8))),
            ("h2", _iso(now - timedelta(hours=6))),
            ("h2", _iso(now - timedelta(hours=1))),
        ],
    )
    assert "uk-1" not in {r["device_id"] for r in db.get_fleet_health_deltas()}


# --------------------------------------------------------------------------- #
# Part C — GET /devices/{id}/health + staleness overlay
# --------------------------------------------------------------------------- #
def test_health_endpoint_404_unknown(client) -> None:
    assert client.get("/api/v1/devices/nope/health").status_code == 404


def test_health_endpoint_returns_blob(client) -> None:
    _seed_device("api-1", "API-1", _blob(state="h1", confidence="high"))
    r = client.get("/api/v1/devices/api-1/health")
    assert r.status_code == 200
    assert r.json()["state"] == "h1"


def test_health_endpoint_no_health_key_does_not_500(client) -> None:
    db.touch_device("api-2", _iso(_now()), "0.1.0", hostname="API-2")
    db.store_scores("api-2", _iso(_now()), {"risk": {}})  # pre-Ф6 blob: no health key
    r = client.get("/api/v1/devices/api-2/health")
    assert r.status_code == 200
    assert r.json().get("available") is False


def test_staleness_over_3_days_caps_confidence(client) -> None:
    ts = _iso(_now() - timedelta(days=5))
    _seed_device("api-3", "API-3", _blob(state="h1", confidence="high"), ts=ts)
    j = client.get("/api/v1/devices/api-3/health").json()
    assert j["confidence"] == "low"
    assert j["state"] == "h1"  # state NOT gutted at the 3-day tier
    assert any("устарел" in m for m in j["missing_evidence"])


def test_staleness_over_10_days_goes_unknown(client) -> None:
    ts = _iso(_now() - timedelta(days=12))
    _seed_device("api-4", "API-4", _blob(state="h1", band="good", confidence="high"), ts=ts)
    j = client.get("/api/v1/devices/api-4/health").json()
    assert j["state"] == "unknown" and j["band"] == "unknown" and j["confidence"] == "unknown"
    assert any("недостоверна" in m for m in j["blind_spots"])
    # the stored blob itself is NOT rewritten (overlay is response-only)
    assert _latest_health("api-4")["state"] == "h1"
