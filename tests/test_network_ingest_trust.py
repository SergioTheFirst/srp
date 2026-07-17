"""Phase-2 invariants: 'network' is a gated trust domain with a range validator;
day-1 health axes stay independent of it."""

from __future__ import annotations

import pytest
from server.trust.domains import DOMAIN_SOURCES
from server.trust.states import SemanticStatus
from server.trust.validators import validate_source
from tests.conftest import healthy

pytestmark = pytest.mark.integration


def _sh(status: str) -> dict:
    return {"status": status, "collected_at": "2026-06-10T00:00:00+00:00"}


def test_network_is_a_trust_domain():
    assert DOMAIN_SOURCES["network"] == {"required": ["network"], "optional": []}


@pytest.mark.parametrize(
    "reading,expected",
    [
        ({"quality": [{"loss_pct": 0.0, "latency_ms": 1.0}]}, SemanticStatus.PLAUSIBLE),
        ({"quality": [{"loss_pct": 150.0}]}, SemanticStatus.IMPLAUSIBLE),
        ({"quality": [{"latency_ms": -5.0}]}, SemanticStatus.IMPLAUSIBLE),
        ({"signal_pcts": [200]}, SemanticStatus.IMPLAUSIBLE),
        ({}, SemanticStatus.PLAUSIBLE),
    ],
)
def test_validate_network_ranges(reading, expected):
    status, _ = validate_source("network", reading, None)
    assert status is expected


def _net_env(did, loss=0.0):
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
    payload["network_quality"] = [
        {
            "target_kind": "gateway",
            "target": "192.168.1.1",
            "latency_ms": 1.0,
            "loss_pct": loss,
            "samples": 3,
        }
    ]
    sh = {
        "storage_reliability": _sh("ok"),
        "reliability": _sh("ok"),
        "boot_time": _sh("ok"),
        "network": _sh("ok"),
    }
    return {
        "device_id": did,
        "agent_version": "0.1.0",
        "msg_type": "historical",
        "payload": payload,
        "source_health": sh,
    }


def test_network_domain_trusted_and_axis_scored(client):
    from server import db

    resp = client.post("/api/v1/ingest", json=_net_env("net2-ok"))
    assert resp.status_code == 200, resp.text
    trust = db.get_trust("net2-ok")
    assert trust["domains"]["network"]["state"] == "trusted"
    s100 = db.get_device("net2-ok")["scores"]["risk"]["score100"]
    assert s100["network_risk"]["value"] == 0.0
    assert s100["network_risk"]["confidence"] == "medium"
    # day-1 health axes never depend on the network domain
    assert s100["reliability"]["value"] is not None
    assert s100["wear"]["value"] is not None


def test_implausible_quality_gates_axis_but_not_day1(client):
    from server import db

    resp = client.post("/api/v1/ingest", json=_net_env("net2-bad", loss=500.0))
    assert resp.status_code == 200, resp.text
    trust = db.get_trust("net2-bad")
    assert trust["domains"]["network"]["state"] == "unknown"
    s100 = db.get_device("net2-bad")["scores"]["risk"]["score100"]
    assert s100["network_risk"]["value"] is None  # gate-failed -> withheld
    assert s100["reliability"]["value"] is not None  # day-1 untouched


def test_old_agent_without_network_reads_no_data_and_lower_observability(client):
    from server import db

    payload = healthy("historical")
    sh = {
        "storage_reliability": _sh("ok"),
        "reliability": _sh("ok"),
        "boot_time": _sh("ok"),
    }
    env = {
        "device_id": "net2-old",
        "agent_version": "0.1.0",
        "msg_type": "historical",
        "payload": payload,
        "source_health": sh,
    }
    assert client.post("/api/v1/ingest", json=env).status_code == 200
    s100 = db.get_device("net2-old")["scores"]["risk"]["score100"]
    assert s100["network_risk"]["value"] is None
    assert "нет сетевой телеметрии" in s100["network_risk"]["missing_evidence"]
    obs = s100["observability"]["value"]
    assert obs is not None and obs < 100.0  # the network blind spot now counts (D12)
