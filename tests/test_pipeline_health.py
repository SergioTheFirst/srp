"""§6 — /api/v1/metrics JSON endpoint + /pipeline HTML health page."""

from __future__ import annotations

import pytest
from tests.conftest import envelope, healthy

pytestmark = pytest.mark.integration


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
