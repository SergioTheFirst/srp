"""Integration: trust evaluation on ingest (server/pipeline.evaluate_trust).

Drives the real ingest path through TestClient against a throwaway DB, posting
envelopes that carry `source_health`, then asserting the per-domain trust the
server stored. Pins: required-source failure -> domain UNKNOWN; battery N/A from
payload.present; and accumulation of trust across message types.
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


def test_battery_not_present_is_not_applicable(client):
    from server import db

    payload = healthy("historical")
    payload["battery"] = {"present": False}
    sh = {
        "storage_reliability": _sh("ok"),
        "battery": _sh("ok"),
        "reliability": _sh("ok"),
        "boot_time": _sh("ok"),
    }
    client.post("/api/v1/ingest", json=_env("dev-bat", "historical", payload, sh))
    trust = db.get_trust("dev-bat")
    assert trust["domains"]["battery"]["state"] == "not_applicable"


def test_storage_blocked_makes_storage_unknown(client):
    from server import db

    sh = {
        "storage_reliability": _sh("blocked"),
        "battery": _sh("ok"),
        "reliability": _sh("ok"),
        "boot_time": _sh("ok"),
    }
    client.post("/api/v1/ingest", json=_env("dev-st", "historical", healthy("historical"), sh))
    trust = db.get_trust("dev-st")
    assert trust["domains"]["storage"]["state"] == "unknown"
    assert trust["sources"]["storage_reliability"]["state"] == "unavailable"


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
                "battery": _sh("ok"),
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
