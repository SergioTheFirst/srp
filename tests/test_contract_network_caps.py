"""Contract boundary caps on Phase-1 network lists (Ф2 review follow-up).

max_length on the HistoricalPayload network lists is the real ingest boundary:
one inflated payload is rejected at validation instead of being stored and
re-capped on every read (db.get_network_snapshots read-side caps stay as
defense in depth). Agent-side caps stay strictly <= contract caps, so a
compliant agent can never be rejected by its own server.
"""

from __future__ import annotations

import pytest
from client.collectors import network
from pydantic import ValidationError
from shared.schema import (
    NET_ADAPTERS_MAX,
    NET_CONNECTIONS_MAX,
    NET_NEIGHBORS_MAX,
    NET_QUALITY_MAX,
    NET_ROUTES_MAX,
    HistoricalPayload,
)

_ITEMS = {
    "network_adapters": {"name": "eth0"},
    "network_neighbors": {"ip": "192.168.1.10", "mac": "AA-BB-CC-DD-EE-FF"},
    "network_connections": {"local_ip": "192.168.1.2", "remote_ip": "192.168.1.3"},
    "network_quality": {"target_kind": "gateway", "target": "192.168.1.1"},
    "network_routes": {"dest": "10.20.0.0/16", "next_hop": "10.0.85.1"},
}
_CAPS = {
    "network_adapters": NET_ADAPTERS_MAX,
    "network_neighbors": NET_NEIGHBORS_MAX,
    "network_connections": NET_CONNECTIONS_MAX,
    "network_quality": NET_QUALITY_MAX,
    "network_routes": NET_ROUTES_MAX,
}


# --------------------------------------------------------------------------- #
# Contract: at-cap accepted, over-cap rejected
# --------------------------------------------------------------------------- #
@pytest.mark.unit
@pytest.mark.parametrize("field", sorted(_CAPS))
def test_payload_at_cap_is_valid(field):
    payload = HistoricalPayload(**{field: [_ITEMS[field]] * _CAPS[field]})
    assert len(getattr(payload, field)) == _CAPS[field]


@pytest.mark.unit
@pytest.mark.parametrize("field", sorted(_CAPS))
def test_payload_over_cap_is_rejected(field):
    with pytest.raises(ValidationError):
        HistoricalPayload(**{field: [_ITEMS[field]] * (_CAPS[field] + 1)})


# --------------------------------------------------------------------------- #
# Agent caps never exceed the contract caps (a compliant agent can't be 422'd)
# --------------------------------------------------------------------------- #
@pytest.mark.unit
def test_agent_caps_within_contract_caps():
    assert network._MAX_ADAPTERS <= NET_ADAPTERS_MAX
    assert network._MAX_NEIGHBORS <= NET_NEIGHBORS_MAX
    assert network._MAX_CONNECTIONS <= NET_CONNECTIONS_MAX
    assert network._MAX_QUALITY <= NET_QUALITY_MAX
    assert network._MAX_ROUTES <= NET_ROUTES_MAX


@pytest.mark.unit
def test_collect_network_caps_adapters_and_quality(monkeypatch):
    from client.collectors.ps import PsResult

    oversized = {
        "adapters": [{"name": f"a{i}", "iftype": 6} for i in range(network._MAX_ADAPTERS + 40)],
        "neighbors": [],
        "connections": [],
        "quality": [
            {"target_kind": "gateway", "target": f"10.0.0.{i}", "latency_ms": 1}
            for i in range(network._MAX_QUALITY + 40)
        ],
    }
    monkeypatch.setattr(network, "run_ps", lambda *a, **k: PsResult("ok", oversized))
    payload, _ = network.collect_network()
    assert payload is not None
    assert len(payload["network_adapters"]) == network._MAX_ADAPTERS
    assert len(payload["network_quality"]) == network._MAX_QUALITY
    HistoricalPayload(**payload)  # the capped payload passes the contract
