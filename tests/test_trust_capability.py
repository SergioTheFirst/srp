"""Newly-blocked (regressed) source detection — Plan 3e.

A source that previously delivered (has a stored last-good) and now reports a
collector failure is flagged `regressed`, distinct from a source never seen.
"""

from __future__ import annotations

import pytest
from tests.conftest import healthy

pytestmark = pytest.mark.integration


def _sh(status: str) -> dict:
    return {"status": status, "collected_at": "2026-05-31T00:00:00+00:00"}


def _env(device_id: str, msg_type: str, payload: dict, source_health: dict) -> dict:
    return {
        "device_id": device_id,
        "agent_version": "0.1.0",
        "msg_type": msg_type,
        "payload": payload,
        "source_health": source_health,
    }


def _hb(free_space_status: str) -> dict:
    return {"free_space": _sh(free_space_status), "throttle": _sh("ok"), "disk_latency": _sh("ok")}


def test_source_regressed_after_good_then_blocked(client):
    from server import db

    client.post("/api/v1/ingest", json=_env("dr", "heartbeat", healthy("heartbeat"), _hb("ok")))
    client.post(
        "/api/v1/ingest", json=_env("dr", "heartbeat", healthy("heartbeat"), _hb("blocked"))
    )
    trust = db.get_trust("dr")
    assert trust["sources"]["free_space"]["regressed"] is True
    # a source that stayed ok is not regressed
    assert trust["sources"]["throttle"]["regressed"] is False


def test_source_not_regressed_when_never_good(client):
    from server import db

    sh = {
        "storage_reliability": _sh("blocked"),
        "reliability": _sh("ok"),
        "boot_time": _sh("ok"),
    }
    client.post("/api/v1/ingest", json=_env("dn", "historical", healthy("historical"), sh))
    trust = db.get_trust("dn")
    assert trust["sources"]["storage_reliability"]["regressed"] is False


def test_regressed_surfaced_in_scores_and_dashboard(client):
    client.post("/api/v1/ingest", json=_env("ds", "heartbeat", healthy("heartbeat"), _hb("ok")))
    client.post(
        "/api/v1/ingest", json=_env("ds", "heartbeat", healthy("heartbeat"), _hb("blocked"))
    )
    dev = client.get("/api/v1/devices/ds").json()
    assert "free_space" in dev["scores"]["risk"]["regressed_sources"]
    page = client.get("/device/ds")
    assert page.status_code == 200
    assert "пропали" in page.text
