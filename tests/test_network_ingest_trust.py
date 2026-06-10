"""Phase-1 invariants: 'network' is recorded but ungated — it never gates scores."""

from __future__ import annotations

import pytest
from server.trust.domains import DOMAIN_SOURCES
from server.trust.states import SemanticStatus
from server.trust.validators import validate_source
from tests.conftest import healthy

pytestmark = pytest.mark.integration


def _sh(status: str) -> dict:
    return {"status": status, "collected_at": "2026-06-10T00:00:00+00:00"}


def test_network_not_a_trust_domain():
    assert "network" not in DOMAIN_SOURCES


def test_validate_source_network_is_unchecked():
    status, reason = validate_source("network", {"adapters": 1}, None)
    assert status is SemanticStatus.UNCHECKED
    assert reason is None


def test_network_source_recorded_but_ungated(client):
    """Ingest historical carrying network data + a 'network' source: it is stored
    and recorded in lineage, but is NOT a trust domain and does not block scores."""
    from server import db

    payload = healthy("historical")
    payload["network_adapters"] = [
        {
            "name": "Ethernet",
            "kind": "ethernet",
            "up": True,
            "ipv4": ["192.168.1.5"],
            "gateway": "192.168.1.1",
        }
    ]
    sh = {
        "storage_reliability": _sh("ok"),
        "battery": _sh("ok"),
        "reliability": _sh("ok"),
        "boot_time": _sh("ok"),
        "network": _sh("ok"),
    }
    env = {
        "device_id": "net-trust-001",
        "agent_version": "0.1.0",
        "msg_type": "historical",
        "payload": payload,
        "source_health": sh,
    }
    resp = client.post("/api/v1/ingest", json=env)
    assert resp.status_code == 200, resp.text
    assert resp.json()["scores_updated"] is True  # day-1 scores still computed

    trust = db.get_trust("net-trust-001")
    assert "network" in trust["sources"]  # recorded in lineage
    assert "network" not in trust["domains"]  # but never a gating domain

    hist = db.get_historical("net-trust-001")
    assert hist["network_adapters"][0]["gateway"] == "192.168.1.1"
