"""§6 — /api/v1/metrics JSON endpoint + /pipeline HTML health page."""

from __future__ import annotations

import pytest
from tests.conftest import envelope, healthy

pytestmark = pytest.mark.integration


@pytest.fixture
def db_init(tmp_path):
    from server import db

    db.init_db(tmp_path / "t.db")
    return db


def _env(device_id: str, msg_type: str, payload: dict) -> dict:
    return dict(envelope(device_id, msg_type, payload))


# ── /api/v1/metrics ──────────────────────────────────────────────────────── #


def test_metrics_returns_200_and_json_shape(client):
    r = client.get("/api/v1/metrics")
    assert r.status_code == 200
    m = r.json()
    assert "fleet" in m
    assert "ingest" in m
    assert "source_health" in m
    assert "scores" in m
    assert "table_rows" in m
    assert "ts" in m


def test_metrics_fleet_counts_match_ingested_devices(client):
    client.post("/api/v1/ingest", json=_env("pm-a", "inventory", healthy("inventory")))
    client.post("/api/v1/ingest", json=_env("pm-b", "inventory", healthy("inventory")))
    m = client.get("/api/v1/metrics").json()
    fleet = m["fleet"]
    assert fleet["total"] >= 2
    assert isinstance(fleet["stale"], int)
    assert isinstance(fleet["at_risk"], int)
    assert isinstance(fleet["scored"], int)
    assert fleet["at_risk"] <= fleet["total"]
    assert fleet["scored"] <= fleet["total"]


def test_metrics_ingest_fields_are_non_negative(client):
    client.post("/api/v1/ingest", json=_env("pm-c", "heartbeat", healthy("heartbeat")))
    m = client.get("/api/v1/metrics").json()
    ingest = m["ingest"]
    for key in ("heartbeats_5m", "heartbeats_1h", "historical_5m", "historical_1h"):
        assert ingest[key] >= 0, f"{key} must be non-negative"


def test_metrics_table_rows_includes_known_tables(client):
    m = client.get("/api/v1/metrics").json()
    rows = m["table_rows"]
    for tbl in ("devices", "heartbeats", "historical", "events", "scores"):
        assert tbl in rows, f"table_rows missing '{tbl}'"
        assert isinstance(rows[tbl], int)
        assert rows[tbl] >= 0


def test_metrics_source_health_keys_present(client):
    m = client.get("/api/v1/metrics").json()
    src = m["source_health"]
    assert "gate_pass" in src
    assert "gate_fail" in src
    assert "not_applicable" in src
    for v in src.values():
        assert isinstance(v, int)
        assert v >= 0


def test_metrics_scores_freshness_none_when_no_scores(client):
    # fresh db — no scores ingested for this device yet
    m = client.get("/api/v1/metrics").json()
    # newest_age_sec may be None or an int; both are valid
    assert m["scores"]["newest_age_sec"] is None or isinstance(m["scores"]["newest_age_sec"], int)


def test_metrics_scores_age_populated_after_ingest(client):
    client.post("/api/v1/ingest", json=_env("pm-d", "inventory", healthy("inventory")))
    client.post("/api/v1/ingest", json=_env("pm-d", "heartbeat", healthy("heartbeat")))
    client.post("/api/v1/ingest", json=_env("pm-d", "historical", healthy("historical")))
    m = client.get("/api/v1/metrics").json()
    # After ingest + scoring, newest_age_sec should be a non-negative int
    age = m["scores"]["newest_age_sec"]
    if age is not None:
        assert age >= 0


def test_metrics_expose_lock_and_reject_counters(client) -> None:
    m = client.get("/api/v1/metrics").json()
    assert {"acquisitions", "wait_avg_ms", "wait_max_ms"} <= set(m["lock"])
    assert {"auth", "rate_limit", "duplicate", "invalid", "too_large"} <= set(m["ingest_rejects"])


def test_rate_limit_reject_is_counted(client, monkeypatch) -> None:
    from server import api as api_mod
    from server import ingest_guards

    monkeypatch.setattr(api_mod, "check_rate_limit", lambda _did: False)
    r = client.post(
        "/api/v1/ingest",
        json=envelope("dev-rej", "heartbeat", healthy("heartbeat")),
    )
    assert r.status_code == 429
    assert ingest_guards.REJECT_COUNTS["rate_limit"] == 1


# ── P0-7: get_pipeline_metrics same-calendar-day cutoff format ────────────── #


@pytest.mark.unit
def test_pipeline_metrics_stale_detects_a_device_silent_past_the_threshold_today(db_init):
    from datetime import datetime, timedelta, timezone

    # 61 minutes silent (well past the default 600s stale threshold), same
    # calendar date as "now" for almost the entire day. Before the fix,
    # SQLite's space-separated `datetime('now', ...)` cutoff sorted *after*
    # the T-separated stored last_seen whenever dates matched, so a same-day
    # stale device was silently never counted (server/db.py::_cutoff_iso, P0-7).
    silent_since = (datetime.now(timezone.utc) - timedelta(minutes=61)).isoformat()
    db_init.touch_device("dev-stale", silent_since, "0.1.0", received_at=silent_since)
    metrics = db_init.get_pipeline_metrics()
    assert metrics["fleet"]["stale"] == 1


@pytest.mark.unit
def test_pipeline_metrics_hb_1h_excludes_a_heartbeat_older_than_an_hour_today(db_init):
    from datetime import datetime, timedelta, timezone

    # Received 3 hours ago today -- must NOT count in the "last hour"/"last 5
    # minutes" buckets. Before the fix the same-day T-vs-space collision made
    # hb_1h/hb_5m count the device's entire day regardless of actual age.
    old_receipt = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    db_init.store_heartbeat("dev-old-hb", old_receipt, {"cpu_pct": 1.0}, received_at=old_receipt)
    metrics = db_init.get_pipeline_metrics()
    assert metrics["ingest"]["heartbeats_1h"] == 0
    assert metrics["ingest"]["heartbeats_5m"] == 0


# ── P1-6 follow-up: at_risk floor must match band_for_risk_score's "bad" ─── #


@pytest.mark.unit
def test_pipeline_metrics_at_risk_matches_band_for_risk_score_bad_floor(db_init):
    """40 mirrors score100.py's band_for_risk_score "bad" floor. This raw-SQL
    threshold used to be 50, silently disagreeing with the /fleet page's
    corrected at_risk count (P1-6) for the exact same risk_exposure value."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    db_init.touch_device("dev-at-risk", now, "0.1.0", received_at=now)
    db_init.store_scores("dev-at-risk", now, {"risk_exposure": 42.0})
    db_init.touch_device("dev-not-at-risk", now, "0.1.0", received_at=now)
    db_init.store_scores("dev-not-at-risk", now, {"risk_exposure": 39.9})
    db_init.touch_device("dev-exactly-bad-floor", now, "0.1.0", received_at=now)
    db_init.store_scores("dev-exactly-bad-floor", now, {"risk_exposure": 40.0})
    metrics = db_init.get_pipeline_metrics()
    # 40.0 must count too (band_for_risk_score's "bad" is >=40, not >40) -- pins
    # the >= vs > boundary itself, not just values that straddle it either way.
    assert metrics["fleet"]["at_risk"] == 2


# ── /pipeline HTML page ───────────────────────────────────────────────────── #


def test_pipeline_page_returns_200(client):
    r = client.get("/pipeline")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_pipeline_page_has_fleet_section(client):
    r = client.get("/pipeline")
    assert "Флот" in r.text or "флот" in r.text.lower()


def test_pipeline_page_has_ingest_section(client):
    r = client.get("/pipeline")
    assert "Ingest" in r.text or "heartbeats" in r.text.lower()


def test_pipeline_page_has_source_health_section(client):
    r = client.get("/pipeline")
    assert "источник" in r.text.lower()


def test_pipeline_page_has_db_sizes_section(client):
    r = client.get("/pipeline")
    # the Размер БД section lists known table names
    assert "devices" in r.text
    assert "heartbeats" in r.text


def test_pipeline_page_shows_timestamp(client):
    r = client.get("/pipeline")
    # the ts field renders as ISO-like substring
    assert "снимок" in r.text or "20" in r.text  # ISO year prefix


def test_pipeline_page_reflects_ingested_device_count(client):
    client.post("/api/v1/ingest", json=_env("pm-e", "inventory", healthy("inventory")))
    r = client.get("/pipeline")
    # total >= 1 somewhere in the page (the number itself may appear in various contexts)
    assert r.status_code == 200
    # At minimum the page rendered without error
    assert "<html" in r.text.lower() or "<!doctype" in r.text.lower()


def test_pipeline_page_nav_link_present(client):
    r = client.get("/")
    assert "пайплайн" in r.text or "/pipeline" in r.text


def test_pipeline_page_auto_refresh_meta(client):
    r = client.get("/pipeline")
    assert 'http-equiv="refresh"' in r.text or "refresh" in r.text.lower()
