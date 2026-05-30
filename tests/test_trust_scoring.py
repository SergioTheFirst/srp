"""Integration: bayesian risk gated by per-domain trust (Plan 3c).

Posts envelopes carrying `source_health`, then asserts that the scores the server
stores reflect trust: a class whose mapped domain is untrusted is tagged UNKNOWN,
an untrusted identity flags the device, and old agents (no source_health) get
ungated scores unchanged.
"""

from __future__ import annotations

import pytest
from tests.conftest import healthy

pytestmark = pytest.mark.integration


def _sh(status: str) -> dict:
    return {"status": status, "collected_at": "2026-05-30T00:00:00+00:00"}


def _env(device_id: str, msg_type: str, payload: dict, source_health: dict) -> dict:
    return {
        "device_id": device_id,
        "agent_version": "0.1.0",
        "msg_type": msg_type,
        "payload": payload,
        "source_health": source_health,
    }


def _classes(dev: dict) -> dict:
    return {c["name"]: c for c in dev["scores"]["risk"]["classes"]}


_HIST_OK = {
    "storage_reliability": _sh("ok"),
    "battery": _sh("ok"),
    "reliability": _sh("ok"),
    "boot_time": _sh("ok"),
}


def test_storage_class_unknown_when_domain_untrusted(client):
    sh = dict(_HIST_OK, storage_reliability=_sh("blocked"))
    client.post("/api/v1/ingest", json=_env("dev-st", "historical", healthy("historical"), sh))
    dev = client.get("/api/v1/devices/dev-st").json()
    assert _classes(dev)["storage"]["trust"] == "unknown"
    assert dev["scores"]["risk"]["domains"]["storage"]["state"] == "unknown"


def test_storage_class_trusted_when_source_ok(client):
    client.post(
        "/api/v1/ingest", json=_env("dev-ok", "historical", healthy("historical"), _HIST_OK)
    )
    dev = client.get("/api/v1/devices/dev-ok").json()
    assert _classes(dev)["storage"]["trust"] == "trusted"


def test_memory_class_is_ungated(client):
    client.post(
        "/api/v1/ingest", json=_env("dev-mem", "historical", healthy("historical"), _HIST_OK)
    )
    dev = client.get("/api/v1/devices/dev-mem").json()
    assert _classes(dev)["memory"]["trust"] is None


def test_device_untrusted_when_identity_fails(client):
    client.post(
        "/api/v1/ingest",
        json=_env("dev-unt", "inventory", healthy("inventory"), {"identity": _sh("blocked")}),
    )
    dev = client.get("/api/v1/devices/dev-unt").json()
    assert dev["scores"]["risk"]["device_trust"] == "untrusted"


def test_no_source_health_leaves_scores_ungated(client):
    """Backward compat: an old agent (no source_health) gets ungated scores."""
    env = {
        "device_id": "dev-old",
        "agent_version": "0.1.0",
        "msg_type": "historical",
        "payload": healthy("historical"),
    }
    client.post("/api/v1/ingest", json=env)
    dev = client.get("/api/v1/devices/dev-old").json()
    assert "device_trust" not in dev["scores"]["risk"]
    assert "trust" not in _classes(dev)["storage"]
