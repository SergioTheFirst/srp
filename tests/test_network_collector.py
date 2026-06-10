"""Network collector: parser, privacy filter, caps, status, locale (mock run_ps)."""

from __future__ import annotations

import pytest
from client.collectors import historical, network
from client.collectors.ps import PsResult

pytestmark = pytest.mark.unit


def _ok(data):
    return PsResult("ok", data)


_NET_FULL = {
    "adapters": [
        {
            "name": "Ethernet",
            "desc": "Intel I219",
            "mac": "AA-BB-CC-00-11-22",
            "iftype": 6,
            "up": True,
            "link_bps": 1000000000,
            "ipv4": ["192.168.1.5"],
            "ipv6": [],
            "gateway": "192.168.1.1",
            "dns": ["192.168.1.1"],
            "dhcp": True,
        }
    ],
    "neighbors": [
        {"ip": "192.168.1.1", "mac": "AA-BB-CC-00-11-22", "state": "Reachable"},
        {"ip": "224.0.0.22", "mac": "01-00-5E-00-00-16", "state": "Permanent"},  # dropped
    ],
    "connections": [
        {
            "local_ip": "192.168.1.5",
            "local_port": 50515,
            "remote_ip": "192.168.1.10",
            "remote_port": 445,
            "state": "Established",
        },
        {
            "local_ip": "192.168.1.5",
            "local_port": 51000,
            "remote_ip": "140.82.112.3",
            "remote_port": 443,
            "state": "Established",
        },  # external -> dropped
    ],
    "quality": [
        {
            "target_kind": "gateway",
            "target": "192.168.1.1",
            "latency_ms": 1.4,
            "loss_pct": 0.0,
            "samples": 3,
        }
    ],
}


# --------------------------------------------------------------------------- #
# Parsers + privacy filter
# --------------------------------------------------------------------------- #
def test_parse_adapter_numeric_and_kind():
    raw = {
        "name": "Ethernet",
        "desc": "Intel I219",
        "mac": "AA-BB-CC-00-11-22",
        "iftype": 6,
        "up": True,
        "link_bps": 1000000000,
        "ipv4": ["192.168.1.5"],
        "ipv6": [],
        "gateway": "192.168.1.1",
        "dns": ["192.168.1.1", ""],
        "dhcp": True,
    }
    a = network._parse_adapter(raw)
    assert a["kind"] == "ethernet"
    assert a["up"] is True
    assert a["link_mbps"] == 1000.0
    assert a["ipv4"] == ["192.168.1.5"]
    assert a["dns"] == ["192.168.1.1"]  # empty string dropped


def test_parse_adapter_wifi_iftype():
    a = network._parse_adapter({"name": "Wi-Fi", "iftype": 71, "up": False})
    assert a["kind"] == "wifi"
    assert a["up"] is False
    assert a["link_mbps"] is None


def test_parse_adapter_rejects_non_dict():
    assert network._parse_adapter(None) is None
    assert network._parse_adapter("nope") is None


def test_connection_keeps_internal():
    raw = {
        "local_ip": "192.168.1.5",
        "local_port": 50515,
        "remote_ip": "192.168.1.10",
        "remote_port": 445,
        "state": "Established",
    }
    c = network._parse_connection(raw)
    assert c is not None
    assert c["remote_ip"] == "192.168.1.10"
    assert c["remote_port"] == 445


def test_connection_drops_external():
    raw = {
        "local_ip": "192.168.1.5",
        "local_port": 51000,
        "remote_ip": "140.82.112.3",
        "remote_port": 443,
        "state": "Established",
    }
    assert network._parse_connection(raw) is None


def test_connection_drops_loopback_and_listen():
    assert (
        network._parse_connection(
            {
                "local_ip": "127.0.0.1",
                "local_port": 1,
                "remote_ip": "127.0.0.1",
                "remote_port": 1,
                "state": "Established",
            }
        )
        is None
    )
    assert (
        network._parse_connection(
            {
                "local_ip": "0.0.0.0",
                "local_port": 135,
                "remote_ip": "0.0.0.0",
                "remote_port": 0,
                "state": "Listen",
            }
        )
        is None
    )


def test_neighbor_drops_broadcast_and_multicast():
    assert (
        network._parse_neighbor(
            {"ip": "192.168.1.255", "mac": "FF-FF-FF-FF-FF-FF", "state": "Permanent"}
        )
        is None
    )
    assert (
        network._parse_neighbor(
            {"ip": "224.0.0.22", "mac": "01-00-5E-00-00-16", "state": "Permanent"}
        )
        is None
    )


def test_neighbor_keeps_internal():
    n = network._parse_neighbor(
        {"ip": "192.168.1.1", "mac": "AA-BB-CC-00-11-22", "state": "Reachable"}
    )
    assert n == {"ip": "192.168.1.1", "mac": "AA-BB-CC-00-11-22", "state": "Reachable"}


def test_parse_quality_numbers():
    q = network._parse_quality(
        {
            "target_kind": "gateway",
            "target": "192.168.1.1",
            "latency_ms": 1.4,
            "loss_pct": 0.0,
            "samples": 3,
        }
    )
    assert q == {
        "target_kind": "gateway",
        "target": "192.168.1.1",
        "latency_ms": 1.4,
        "loss_pct": 0.0,
        "samples": 3,
    }


# --------------------------------------------------------------------------- #
# collect_network() orchestration + status
# --------------------------------------------------------------------------- #
def test_collect_network_ok(monkeypatch):
    monkeypatch.setattr(network, "run_ps", lambda *a, **k: _ok(_NET_FULL))
    res = network.collect_network()
    assert res.payload is not None
    assert len(res.payload["network_adapters"]) == 1
    assert len(res.payload["network_neighbors"]) == 1  # multicast dropped
    assert len(res.payload["network_connections"]) == 1  # external dropped
    assert res.source_health[network.NETWORK]["status"] == "ok"


def test_collect_network_blocked(monkeypatch):
    monkeypatch.setattr(network, "run_ps", lambda *a, **k: PsResult("blocked"))
    res = network.collect_network()
    assert res.payload is None
    assert res.source_health[network.NETWORK]["status"] == "blocked"


def test_collect_network_empty_when_all_filtered(monkeypatch):
    monkeypatch.setattr(
        network,
        "run_ps",
        lambda *a, **k: _ok({"adapters": [], "neighbors": [], "connections": [], "quality": []}),
    )
    res = network.collect_network()
    assert res.payload is not None
    assert res.source_health[network.NETWORK]["status"] == "empty"


def test_collect_network_caps_neighbors(monkeypatch):
    many = {
        "adapters": [],
        "connections": [],
        "quality": [],
        "neighbors": [
            {"ip": f"10.0.0.{i % 254 + 1}", "mac": "AA-BB-CC-00-11-22", "state": "Stale"}
            for i in range(400)
        ],
    }
    monkeypatch.setattr(network, "run_ps", lambda *a, **k: _ok(many))
    res = network.collect_network()
    assert len(res.payload["network_neighbors"]) == network._MAX_NEIGHBORS


# --------------------------------------------------------------------------- #
# Locale invariant
# --------------------------------------------------------------------------- #
def test_adapter_cyrillic_name_passes_through(monkeypatch):
    """A Russian friendly name must survive intact; kind still derives from numeric ifType."""
    data = {
        "adapters": [
            {
                "name": "Подключение Ethernet",
                "desc": "Сетевой адаптер",
                "mac": "AA-BB-CC-00-11-22",
                "iftype": 6,
                "up": True,
                "ipv4": ["10.0.0.5"],
                "dns": [],
                "dhcp": True,
            }
        ],
        "neighbors": [],
        "connections": [],
        "quality": [],
    }
    monkeypatch.setattr(network, "run_ps", lambda *a, **k: _ok(data))
    res = network.collect_network()
    a = res.payload["network_adapters"][0]
    assert a["name"] == "Подключение Ethernet"
    assert a["kind"] == "ethernet"  # from numeric ifType, not text


# --------------------------------------------------------------------------- #
# Fold into collect_historical (certificates-style merge)
# --------------------------------------------------------------------------- #
def test_historical_merges_network(monkeypatch):
    """collect_historical folds network payload + source_health in."""

    def _hist_ps(script, timeout=30):
        if timeout == 120:
            return _ok(
                {"reliability_stability_index": 9.0, "storage": [], "battery": {"present": False}}
            )
        if timeout == 60:
            return _ok({"certificates": []})
        return PsResult("empty")

    monkeypatch.setattr(historical, "run_ps", _hist_ps)
    monkeypatch.setattr(
        historical,
        "collect_network",
        lambda: network.CollectorResult(
            {
                "network_adapters": [{"name": "Ethernet"}],
                "network_neighbors": [],
                "network_connections": [],
                "network_quality": [],
            },
            {network.NETWORK: network.health("ok")},
        ),
    )
    res = historical.collect_historical()
    assert res.payload["network_adapters"] == [{"name": "Ethernet"}]
    assert res.source_health[network.NETWORK]["status"] == "ok"


def test_historical_network_failure_sets_empty_fields(monkeypatch):
    def _hist_ps(script, timeout=30):
        if timeout == 120:
            return _ok(
                {"reliability_stability_index": 9.0, "storage": [], "battery": {"present": False}}
            )
        return _ok({"certificates": []})

    monkeypatch.setattr(historical, "run_ps", _hist_ps)
    monkeypatch.setattr(
        historical,
        "collect_network",
        lambda: network.CollectorResult(None, network.failed([network.NETWORK], "blocked")),
    )
    res = historical.collect_historical()
    assert res.payload["network_adapters"] == []
    assert res.payload["network_connections"] == []


# --------------------------------------------------------------------------- #
# Review hardening: RFC1918-strict privacy filter (spec privacy contract)
# --------------------------------------------------------------------------- #
def test_internal_filter_is_rfc1918_strict():
    """Spec: only 10/8, 172.16/12, 192.168/16 ever leave the agent — stricter
    than ipaddress.is_private (TEST-NET/benchmark/CGNAT/broadcast all out)."""
    assert network._is_internal("10.1.2.3") is True
    assert network._is_internal("172.31.0.9") is True
    assert network._is_internal("192.168.0.1") is True
    assert network._is_internal("203.0.113.5") is False  # TEST-NET-3
    assert network._is_internal("198.18.0.7") is False  # benchmarking
    assert network._is_internal("192.0.2.10") is False  # TEST-NET-1
    assert network._is_internal("100.64.0.1") is False  # CGNAT
    assert network._is_internal("255.255.255.255") is False  # limited broadcast
    assert network._is_internal("169.254.10.10") is False  # link-local
    assert network._is_internal("8.8.8.8") is False  # public


def test_internal_filter_ipv6_rfc1918_only():
    """IPv6: only IPv4-mapped forms unwrap to their v4 address; ULA and global
    v6 are outside the RFC1918-only contract and never serialized."""
    assert network._is_internal("::ffff:192.168.1.7") is True  # v4-mapped, RFC1918
    assert network._is_internal("::ffff:8.8.8.8") is False  # v4-mapped, public
    assert network._is_internal("fd00::1") is False  # ULA
    assert network._is_internal("2001:db8::1") is False  # documentation
    assert network._is_internal("::1") is False  # loopback


def test_neighbor_broadcast_filter_is_mac_based():
    """Honest Phase-1 limitation: a directed-broadcast IP inside RFC1918 is only
    dropped via the FF-FF MAC; with a unicast MAC it passes (subnet-broadcast
    detection needs the prefix length, which the script does not emit)."""
    kept = network._parse_neighbor(
        {"ip": "192.168.1.255", "mac": "AA-BB-CC-00-11-22", "state": "Stale"}
    )
    assert kept is not None


def test_link_speed_unknown_sentinel_is_none():
    """Driver 'speed unknown' sentinels must not render as a ~4.3 Tbps link."""
    a32 = network._parse_adapter({"name": "X", "iftype": 6, "link_bps": 4294967295})
    assert a32["link_mbps"] is None
    a64 = network._parse_adapter({"name": "X", "iftype": 6, "link_bps": 18446744073709551615})
    assert a64["link_mbps"] is None


def test_collect_network_quality_counts_as_present(monkeypatch):
    """A quality-only snapshot is data, not 'empty'."""
    only_q = {
        "adapters": [],
        "neighbors": [],
        "connections": [],
        "quality": [
            {
                "target_kind": "gateway",
                "target": "192.168.1.1",
                "latency_ms": 1.0,
                "loss_pct": 0.0,
                "samples": 3,
            }
        ],
    }
    monkeypatch.setattr(network, "run_ps", lambda *a, **k: _ok(only_q))
    res = network.collect_network()
    assert res.source_health[network.NETWORK]["status"] == "ok"
