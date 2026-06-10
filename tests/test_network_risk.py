"""network_risk engine: gateway quality leads, APIPA standalone, ICMP honesty."""

from __future__ import annotations

import pytest
from server.analytics.network_risk import compute_network_risk

pytestmark = pytest.mark.unit


def _hist(adapters=None, quality=None):
    return {"network_adapters": adapters or [], "network_quality": quality or []}


def _gw(loss=0.0, lat=1.0, target="192.168.1.1"):
    return {
        "target_kind": "gateway",
        "target": target,
        "latency_ms": lat,
        "loss_pct": loss,
        "samples": 3,
    }


def _dns(loss=0.0, lat=2.0, target="192.168.1.53"):
    return {
        "target_kind": "dns",
        "target": target,
        "latency_ms": lat,
        "loss_pct": loss,
        "samples": 3,
    }


def _eth(ip="192.168.1.10", up=True, **kw):
    return {
        "name": "Ethernet",
        "kind": "ethernet",
        "up": up,
        "ipv4": [ip],
        "gateway": "192.168.1.1",
        **kw,
    }


def test_untrusted_withheld():
    s = compute_network_risk(_hist([_eth()], [_gw()]), device_trust="untrusted")
    assert s.value is None and s.band == "unknown"


def test_no_telemetry_unknown_even_when_domain_gate_failed():
    s = compute_network_risk({}, domain_state="unknown")
    assert s.value is None
    assert "нет сетевой телеметрии" in s.missing_evidence  # data-absence wins (order)


def test_domain_gate_failed_with_data_withheld():
    s = compute_network_risk(_hist([_eth()], [_gw()]), domain_state="unknown")
    assert s.value is None
    assert "гейт доверия" in s.reason


def test_healthy_gateway_confident_zero_capped_medium():
    s = compute_network_risk(_hist([_eth()], [_gw(loss=0.0, lat=1.0)]))
    assert s.value == 0.0 and s.band == "good"
    assert s.confidence == "medium"  # D11 cap
    assert s.reason == "связь со шлюзом в норме"
    assert any("за пределами шлюза" in m for m in s.missing_evidence)


@pytest.mark.parametrize("loss,expected", [(7.0, 15.0), (25.0, 30.0)])
def test_gateway_partial_loss_grades(loss, expected):
    s = compute_network_risk(_hist([_eth()], [_gw(loss=loss)]))
    assert s.value == expected


def test_gateway_full_loss_with_other_reply_is_failure():
    s = compute_network_risk(_hist([_eth()], [_gw(loss=100.0, lat=None), _dns(loss=0.0)]))
    assert s.value == 45.0 and s.band == "bad"


def test_all_probes_lost_is_icmp_ambiguity_not_alarm():
    s = compute_network_risk(
        _hist([_eth()], [_gw(loss=100.0, lat=None), _dns(loss=100.0, lat=None)])
    )
    assert s.value == 0.0
    assert s.confidence == "low"
    assert any("ICMP" in m for m in s.missing_evidence)
    assert s.factors == []
    assert s.source_lineage["icmp_blocked"] is True


def test_gateway_latency_confirmation():
    assert compute_network_risk(_hist([_eth()], [_gw(loss=0.0, lat=120.0)])).value == 15.0
    assert compute_network_risk(_hist([_eth()], [_gw(loss=0.0, lat=35.0)])).value == 8.0


def test_dns_partial_counts_once_full_loss_ignored():
    s = compute_network_risk(
        _hist([_eth()], [_gw(loss=0.0), _dns(loss=50.0), _dns(loss=60.0, target="192.168.1.54")])
    )
    assert s.value == 8.0  # worst DNS only
    s2 = compute_network_risk(_hist([_eth()], [_gw(loss=0.0), _dns(loss=100.0, lat=None)]))
    assert s2.value == 0.0  # DNS boxes commonly drop ICMP
    assert s2.source_lineage["dns_full_loss_ignored"] == 1


def test_apipa_standalone_failure():
    s = compute_network_risk(_hist([_eth(ip="169.254.10.20")], []))
    assert s.value == 35.0
    assert any("APIPA" in f["label"] for f in s.factors)
    assert s.confidence == "low"  # no quality measurement


def test_wifi_weak_signal():
    wifi = {
        "name": "Wi-Fi",
        "kind": "wifi",
        "up": True,
        "ipv4": ["192.168.1.20"],
        "gateway": "192.168.1.1",
        "signal_pct": 20,
    }
    s = compute_network_risk(_hist([wifi], [_gw(loss=0.0)]))
    assert s.value == 12.0
    wifi2 = {**wifi, "signal_pct": 40}
    assert compute_network_risk(_hist([wifi2], [_gw(loss=0.0)])).value == 6.0


def test_adapters_only_low_confidence():
    s = compute_network_risk(_hist([_eth()], []))
    assert s.value == 0.0 and s.confidence == "low"
    assert "не измерено" in s.reason
    assert any("качества" in m for m in s.missing_evidence)


def test_clamped_at_100_and_deterministic():
    h = _hist(
        [_eth(ip="169.254.1.2"), _eth(ip="169.254.1.3")],
        [_gw(loss=100.0, lat=None), _dns(loss=0.0)],
    )
    s1, s2 = compute_network_risk(h), compute_network_risk(h)
    assert s1.value == 100.0 and s1 == s2
