"""Integration: trust evaluation on ingest (server/pipeline.evaluate_trust).

Drives the real ingest path through TestClient against a throwaway DB, posting
envelopes that carry `source_health`, then asserting the per-domain trust the
server stored. Pins: required-source failure -> domain UNKNOWN; a source name the
server does not know (a retired source a stale agent still sends, or a forged
name) -> silently skipped, no trust row, no exception; and accumulation of trust
across message types.
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


def test_heartbeat_throttle_timeout_makes_thermal_unknown(client):
    from server import db

    sh = {"free_space": _sh("ok"), "throttle": _sh("timeout"), "disk_latency": _sh("ok")}
    resp = client.post("/api/v1/ingest", json=_env("dev-hb", "heartbeat", healthy("heartbeat"), sh))
    assert resp.status_code == 200, resp.text
    trust = db.get_trust("dev-hb")
    assert trust is not None
    assert trust["domains"]["disk_fill"]["state"] == "trusted"
    assert trust["domains"]["thermal"]["state"] == "unknown"  # throttle required + timeout
    # lineage records the failing source's derived state
    assert trust["sources"]["throttle"]["state"] == "unavailable"


def test_storage_blocked_makes_storage_unknown(client):
    from server import db

    sh = {
        "storage_reliability": _sh("blocked"),
        "reliability": _sh("ok"),
        "boot_time": _sh("ok"),
    }
    client.post("/api/v1/ingest", json=_env("dev-st", "historical", healthy("historical"), sh))
    trust = db.get_trust("dev-st")
    assert trust["domains"]["storage"]["state"] == "unknown"
    assert trust["sources"]["storage_reliability"]["state"] == "unavailable"


def test_smart_optional_source_contributes_to_storage_domain(client):
    from server import db

    payload = healthy("historical")
    payload["storage"][0]["serial_hash"] = "diskhash1"
    payload["storage"][0]["nvme_media_errors"] = 0
    sh = {
        "storage_reliability": _sh("ok"),
        "smart": _sh("ok"),
        "reliability": _sh("ok"),
        "boot_time": _sh("ok"),
    }
    resp = client.post("/api/v1/ingest", json=_env("dev-smart", "historical", payload, sh))
    assert resp.status_code == 200, resp.text
    trust = db.get_trust("dev-smart")
    assert trust["domains"]["storage"]["state"] == "trusted"
    assert "smart" in trust["domains"]["storage"]["contributing"]
    assert trust["sources"]["smart"]["state"] == "ok"


def test_smart_blocked_optional_source_is_dropped_not_fatal(client):
    from server import db

    sh = {
        "storage_reliability": _sh("ok"),
        "smart": _sh("blocked"),
        "reliability": _sh("ok"),
        "boot_time": _sh("ok"),
    }
    resp = client.post(
        "/api/v1/ingest", json=_env("dev-smart2", "historical", healthy("historical"), sh)
    )
    assert resp.status_code == 200, resp.text
    trust = db.get_trust("dev-smart2")
    # required source alone still trusts the domain; optional smart is only dropped
    assert trust["domains"]["storage"]["state"] == "trusted"
    assert "smart" in trust["domains"]["storage"]["dropped"]


def test_trust_accumulates_across_message_types(client):
    from server import db

    client.post(
        "/api/v1/ingest",
        json=_env(
            "dev-acc",
            "heartbeat",
            healthy("heartbeat"),
            {"free_space": _sh("ok"), "throttle": _sh("ok"), "disk_latency": _sh("ok")},
        ),
    )
    client.post(
        "/api/v1/ingest",
        json=_env(
            "dev-acc",
            "historical",
            healthy("historical"),
            {
                "storage_reliability": _sh("ok"),
                "reliability": _sh("ok"),
                "boot_time": _sh("ok"),
            },
        ),
    )
    trust = db.get_trust("dev-acc")
    # heartbeat-owned domains survive the later historical ingest (accumulation, not overwrite)
    assert trust["domains"]["disk_fill"]["state"] == "trusted"
    assert trust["domains"]["thermal"]["state"] == "trusted"
    # historical-owned domains present too
    assert trust["domains"]["storage"]["state"] == "trusted"
    assert trust["domains"]["os_stability"]["state"] == "trusted"
    assert trust["domains"]["boot"]["state"] == "trusted"


def test_unknown_source_and_payload_key_are_ignored_not_fatal(client):
    """A stale/legacy agent may still report a source_health key (or a payload
    field) this server no longer knows -- e.g. a retired source, or a forged
    name. Ingest must accept the envelope (200), silently drop the unknown
    source (no trust row, no exception), and keep processing every known
    source exactly as if the unknown one were never there.
    """
    from server import db

    payload = healthy("historical")
    payload["some_retired_field_z"] = {"whatever": 1}  # extra="allow" tolerance
    sh = {
        "storage_reliability": _sh("ok"),
        "reliability": _sh("ok"),
        "boot_time": _sh("ok"),
        "legacy_source_x": _sh("ok"),  # unknown to this server
    }
    resp = client.post("/api/v1/ingest", json=_env("dev-legacy", "historical", payload, sh))
    assert resp.status_code == 200, resp.text

    trust = db.get_trust("dev-legacy")
    assert "legacy_source_x" not in trust["sources"]  # no trust row for the unknown source
    # known sources in the same envelope are unaffected
    assert trust["sources"]["storage_reliability"]["state"] == "ok"
    assert trust["domains"]["storage"]["state"] == "trusted"
    assert trust["domains"]["os_stability"]["state"] == "trusted"
    assert trust["domains"]["boot"]["state"] == "trusted"
