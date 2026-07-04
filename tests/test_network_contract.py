"""Contract tests: network fields are additive-optional; no CONTRACT_VERSION bump."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from shared.schema import CONTRACT_VERSION, HistoricalPayload

pytestmark = pytest.mark.unit


def test_historical_payload_valid_without_network_fields():
    """An older agent that sends no network fields must still validate."""
    p = HistoricalPayload(reliability_stability_index=9.1)
    assert p.network_adapters == []
    assert p.network_neighbors == []
    assert p.network_connections == []
    assert p.network_quality == []


def test_network_fields_round_trip():
    p = HistoricalPayload(
        network_adapters=[
            {"name": "Ethernet", "kind": "ethernet", "up": True, "ipv4": ["192.168.1.5"]}
        ],
        network_neighbors=[{"ip": "192.168.1.1", "mac": "AA-BB-CC-00-11-22", "state": "Reachable"}],
        network_connections=[
            {
                "local_ip": "192.168.1.5",
                "local_port": 50515,
                "remote_ip": "192.168.1.10",
                "remote_port": 445,
                "state": "Established",
            }
        ],
        network_quality=[
            {
                "target_kind": "gateway",
                "target": "192.168.1.1",
                "latency_ms": 1.4,
                "loss_pct": 0.0,
                "samples": 3,
            }
        ],
    )
    assert p.network_adapters[0].ipv4 == ["192.168.1.5"]
    assert p.network_connections[0].remote_port == 445
    assert p.network_quality[0].latency_ms == 1.4


def test_contract_version_unchanged():
    assert CONTRACT_VERSION == "0.1.0"


# --------------------------------------------------------------------------- #
# T2: agent-resolved NetBIOS neighbor name (additive; no CONTRACT_VERSION bump) #
# --------------------------------------------------------------------------- #


def test_neighbor_name_and_name_source_round_trip():
    p = HistoricalPayload(
        network_neighbors=[
            {
                "ip": "192.168.9.6",
                "mac": "AA-BB-CC-00-11-22",
                "name": "MEDPOST",
                "name_source": "netbios",
            }
        ]
    )
    assert p.network_neighbors[0].name == "MEDPOST"
    assert p.network_neighbors[0].name_source == "netbios"


def test_neighbor_name_absent_is_fine():
    """An older agent (or a neighbor NBNS never answered) sends no name -> None."""
    p = HistoricalPayload(
        network_neighbors=[{"ip": "192.168.1.1", "mac": "AA-BB-CC-00-11-22", "state": "Reachable"}]
    )
    assert p.network_neighbors[0].name is None
    assert p.network_neighbors[0].name_source is None


def test_neighbor_name_over_max_length_is_rejected():
    with pytest.raises(ValidationError):
        HistoricalPayload(network_neighbors=[{"ip": "192.168.1.1", "name": "X" * 64}])


# --------------------------------------------------------------------------- #
# T3: adapter role/tunnel classification (additive; no CONTRACT_VERSION bump) #
# --------------------------------------------------------------------------- #


def test_adapter_role_and_tunnel_round_trip():
    p = HistoricalPayload(
        network_adapters=[
            {"name": "TAP-Windows Adapter V9", "kind": "tunnel", "role": "tunnel", "tunnel": True}
        ]
    )
    assert p.network_adapters[0].role == "tunnel"
    assert p.network_adapters[0].tunnel is True


def test_adapter_role_and_tunnel_absent_is_fine():
    """An older agent that sends no role/tunnel must still validate."""
    p = HistoricalPayload(network_adapters=[{"name": "Ethernet", "kind": "ethernet"}])
    assert p.network_adapters[0].role is None
    assert p.network_adapters[0].tunnel is None


def test_adapter_role_over_max_length_is_rejected():
    with pytest.raises(ValidationError):
        HistoricalPayload(network_adapters=[{"name": "Ethernet", "role": "X" * 17}])
