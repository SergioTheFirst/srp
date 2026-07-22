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
    "reliability": _sh("ok"),
    "boot_time": _sh("ok"),
}


def test_storage_class_unknown_when_domain_untrusted(client):
    sh = dict(_HIST_OK, storage_reliability=_sh("blocked"))
    client.post("/api/v1/ingest", json=_env("dev-st", "historical", healthy("historical"), sh))
    dev = client.get("/api/v1/devices/dev-st").json()
    assert _classes(dev)["storage"]["trust"] == "unknown"
    assert dev["scores"]["risk"]["domains"]["storage"]["state"] == "unknown"
    # P0-5 (stoperrors.md): a gate-failed domain must not leak a confident
    # number over the wire -- "trust":"unknown" used to sit right next to a
    # real probability/level in this exact response.
    assert _classes(dev)["storage"]["probability"] is None
    assert _classes(dev)["storage"]["level"] == "unknown"


def test_storage_class_trusted_when_source_ok(client):
    client.post(
        "/api/v1/ingest", json=_env("dev-ok", "historical", healthy("historical"), _HIST_OK)
    )
    dev = client.get("/api/v1/devices/dev-ok").json()
    assert _classes(dev)["storage"]["trust"] == "trusted"
    assert _classes(dev)["storage"]["probability"] is not None


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
    # contract §7: untrusted identity -> day-1 scores withheld (None), not shown
    assert dev["scores"]["performance"] is None
    assert dev["scores"]["risk_exposure"] is None
    # P0-5: identity-untrusted is a superset gate -- every mapped bayesian
    # class withholds too, regardless of its own domain's individual state.
    classes = _classes(dev)
    # memory has no trust domain of its own (test_memory_class_is_ungated) --
    # device_untrusted is the one thing that must still withhold it, since it
    # is the identity-level superset gate, not a domain lookup.
    for name in ("storage", "power_thermal", "stability", "memory"):
        assert classes[name]["probability"] is None, name
        assert classes[name]["level"] == "unknown", name


def test_no_source_health_low_confidence_score100(client):
    """Backward compat (W0.5): an old agent (no source_health) keeps its ungated
    legacy numbers, but its Score100 is flagged low-confidence -- never silently
    healthy."""
    env = {
        "device_id": "dev-old",
        "agent_version": "0.1.0",
        "msg_type": "historical",
        "payload": healthy("historical"),
    }
    client.post("/api/v1/ingest", json=env)
    dev = client.get("/api/v1/devices/dev-old").json()
    sc = dev["scores"]
    # legacy risk gating untouched (no per-domain trust to apply)
    assert "device_trust" not in sc["risk"]
    assert "trust" not in _classes(dev)["storage"]
    # but the Score100 envelope is present and low/unknown confidence with a reason
    rel = sc["risk"]["score100"]["reliability"]
    assert rel["confidence"] in ("low", "unknown")
    assert "source_health отсутствует" in rel["missing_evidence"]
    # legacy numeric still present for the dashboard
    assert sc["reliability"] is not None
