"""Network collector: parser, privacy filter, caps, status, locale (mock run_ps)."""

from __future__ import annotations

import pytest
from client.collectors import historical, network
from client.collectors.ps import PsResult

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _no_netbios_network(monkeypatch):
    """collect_network() now resolves NetBIOS names for its neighbors (T2); keep
    this suite hermetic -- name resolution itself is covered by
    tests/test_lan_names.py, and the attach-behavior tests below override this."""
    monkeypatch.setattr(network, "resolve_netbios_names", lambda ips, **kw: {})


@pytest.fixture(autouse=True)
def _no_lan_discovery_network(monkeypatch):
    """collect_network() now also relays passive mDNS/SSDP/WSD captures (P1);
    keep this suite hermetic and fast -- _NET_FULL's adapter is role="lan" with
    a real ipv4, so without this stub every test here would open real sockets
    and block for the real listen budget. Collector internals are covered by
    tests/test_lan_discovery.py; the wiring tests below override this."""
    monkeypatch.setattr(network, "collect_lan_discovery", lambda ips, **kw: [])


@pytest.fixture(autouse=True)
def _no_active_scan_network(monkeypatch):
    """collect_network(active_scan=True) now sweeps+rescans (P2); keep this
    suite hermetic by default -- the active_scan=True wiring tests below
    override this with a spy."""
    monkeypatch.setattr(network, "sweep_lan", lambda ips, **kw: 0)


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


# --------------------------------------------------------------------------- #
# T1: routing table -> net_routes filter (RFC1918 dest+next_hop, gateway-skip) #
# --------------------------------------------------------------------------- #
def test_parse_route_keeps_internal_route_via_non_gateway_router():
    r = network._parse_route(
        {"dest": "10.20.0.0/16", "next_hop": "10.0.85.1", "if_index": 4, "metric": 10},
        set(),
    )
    assert r == {"dest": "10.20.0.0/16", "next_hop": "10.0.85.1", "if_index": 4, "metric": 10}


def test_parse_route_drops_default_route_via_gateway():
    assert network._parse_route({"dest": "0.0.0.0/0", "next_hop": "10.0.0.1"}, {"10.0.0.1"}) is None


def test_parse_route_drops_bogon_dest_via_gateway():
    """A bogon TEST-NET destination whose next_hop is the adapter gateway: dropped
    twice over (non-RFC1918 dest AND a gateway next_hop)."""
    r = network._parse_route({"dest": "192.0.2.0/24", "next_hop": "10.0.0.1"}, {"10.0.0.1"})
    assert r is None


def test_parse_route_drops_onlink_route():
    """next_hop 0.0.0.0 (on-link) is not RFC1918 -- dropped by the same check as
    any other non-internal next_hop, no special-casing needed."""
    assert network._parse_route({"dest": "10.0.0.0/24", "next_hop": "0.0.0.0"}, set()) is None


def test_parse_route_drops_public_destination():
    assert network._parse_route({"dest": "8.8.8.0/24", "next_hop": "10.0.0.5"}, set()) is None


def test_parse_route_drops_public_next_hop():
    assert network._parse_route({"dest": "10.0.0.0/24", "next_hop": "8.8.8.8"}, set()) is None


def test_parse_route_malformed_dest_is_skipped_not_crashed():
    assert network._parse_route({"dest": "not-a-cidr", "next_hop": "10.0.0.5"}, set()) is None


def test_parse_route_missing_fields_are_skipped():
    assert network._parse_route({}, set()) is None


def test_parse_route_non_dict_is_skipped():
    assert network._parse_route("garbage", set()) is None


def test_collect_network_keeps_only_the_filtered_routes(monkeypatch):
    data = {
        "adapters": [
            {
                "name": "Ethernet",
                "mac": "AA-BB-CC-00-11-22",
                "iftype": 6,
                "gateway": "10.0.0.1",
                "ipv4": ["10.0.0.5"],
            }
        ],
        "neighbors": [],
        "connections": [],
        "quality": [],
        "routes": [
            {"dest": "10.20.0.0/16", "next_hop": "10.0.85.1", "if_index": 4, "metric": 10},
            {"dest": "0.0.0.0/0", "next_hop": "10.0.0.1", "if_index": 4, "metric": 0},  # gateway
            {"dest": "8.8.8.0/24", "next_hop": "10.0.0.5", "if_index": 4, "metric": 5},  # public
        ],
    }
    monkeypatch.setattr(network, "run_ps", lambda *a, **k: _ok(data))
    res = network.collect_network()
    assert res.payload["network_routes"] == [
        {"dest": "10.20.0.0/16", "next_hop": "10.0.85.1", "if_index": 4, "metric": 10}
    ]


def test_collect_network_caps_routes(monkeypatch):
    many = {
        "adapters": [],
        "neighbors": [],
        "connections": [],
        "quality": [],
        "routes": [
            {
                "dest": f"10.{i % 250 + 1}.0.0/24",
                "next_hop": "10.0.85.1",
                "if_index": 1,
                "metric": 1,
            }
            for i in range(100)
        ],
    }
    monkeypatch.setattr(network, "run_ps", lambda *a, **k: _ok(many))
    res = network.collect_network()
    assert len(res.payload["network_routes"]) == network._MAX_ROUTES


def test_collect_network_routes_only_counts_as_present(monkeypatch):
    """A routes-only snapshot is data, not 'empty'."""
    only_r = {
        "adapters": [],
        "neighbors": [],
        "connections": [],
        "quality": [],
        "routes": [{"dest": "10.20.0.0/16", "next_hop": "10.0.85.1", "if_index": 4, "metric": 10}],
    }
    monkeypatch.setattr(network, "run_ps", lambda *a, **k: _ok(only_r))
    res = network.collect_network()
    assert res.source_health[network.NETWORK]["status"] == "ok"


# --------------------------------------------------------------------------- #
# T3: adapter role classification (VPN/tunnel egress flag)                    #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "name,desc,iftype,expected_role,expected_tunnel",
    [
        # real examples from the architect's own box
        (
            "Local Area Connection 7",
            "TAP-Windows Adapter V9 for OpenVPN Connect",
            6,
            "tunnel",
            True,
        ),
        ("Ethernet 2", "OpenVPN Data Channel Offload", 6, "tunnel", True),
        ("tun2socks Tunnel", "tun2socks Tunnel", 6, "tunnel", True),
        ("Local Area Connection 8", "TAP-Windows Adapter V9", 6, "tunnel", True),  # Outline
        ("Ethernet 3", "WireGuard Tunnel", 6, "tunnel", True),
        ("Tailscale", "Tailscale Tunnel", 6, "tunnel", True),
        ("Ethernet 9", "WAN Miniport (L2TP)", 6, "tunnel", True),
        # plain kind-based fallbacks
        ("Ethernet", "Realtek PCIe GbE Family Controller", 6, "lan", False),
        ("Wi-Fi", "Intel(R) Wi-Fi 6 AX201 160MHz", 71, "wifi", False),
        ("Ethernet 6", "Unknown NIC", 0, "other", False),
        # virtual adapters -- not a tunnel, not a real LAN/Wi-Fi uplink either
        ("vEthernet (Default Switch)", "Hyper-V Virtual Ethernet Adapter", 6, "virtual", False),
        ("Ethernet 4", "VMware Virtual Ethernet Adapter for VMnet8", 6, "virtual", False),
        ("Loopback Pseudo-Interface 1", "Software Loopback Interface 1", 24, "virtual", False),
        (
            "Bluetooth Network Connection",
            "Bluetooth Device (Personal Area Network)",
            6,
            "virtual",
            False,
        ),
        # false-positive guard: "tun" is a substring of "Fortune" -- bare tun/tap
        # tokens are deliberately NOT in the tunnel list (see ponytail note in
        # _adapter_role), so this must classify as a plain LAN adapter.
        ("Ethernet 5", "Fortune Networks Gigabit Adapter", 6, "lan", False),
    ],
)
def test_parse_adapter_classifies_role_and_tunnel(
    name, desc, iftype, expected_role, expected_tunnel
):
    a = network._parse_adapter({"name": name, "desc": desc, "iftype": iftype})
    assert a["role"] == expected_role
    assert a["tunnel"] is expected_tunnel


def test_adapter_role_tunnel_wins_over_kind():
    """Even if the driver reports iftype as wifi, a VPN adapter's name/desc wins."""
    assert network._adapter_role("Ethernet", "OpenVPN Data Channel Offload", "wifi") == (
        "tunnel",
        True,
    )


def test_adapter_role_handles_missing_name_and_desc():
    assert network._adapter_role(None, None, "ethernet") == ("lan", False)
    assert network._adapter_role(None, None, "other") == ("other", False)


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
            return _ok({"reliability_stability_index": 9.0, "storage": []})
        if timeout == 60:
            return _ok({"certificates": []})
        return PsResult("empty")

    monkeypatch.setattr(historical, "run_ps", _hist_ps)
    monkeypatch.setattr(
        historical,
        "collect_network",
        lambda active_scan=False: network.CollectorResult(
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
            return _ok({"reliability_stability_index": 9.0, "storage": []})
        return _ok({"certificates": []})

    monkeypatch.setattr(historical, "run_ps", _hist_ps)
    monkeypatch.setattr(
        historical,
        "collect_network",
        lambda active_scan=False: network.CollectorResult(
            None, network.failed([network.NETWORK], "blocked")
        ),
    )
    res = historical.collect_historical()
    assert res.payload["network_adapters"] == []
    assert res.payload["network_connections"] == []
    assert res.payload["network_routes"] == []


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


# --------------------------------------------------------------------------- #
# T2: agent-side NetBIOS naming attaches name/name_source to neighbors        #
# --------------------------------------------------------------------------- #
def test_collect_network_attaches_netbios_name(monkeypatch):
    monkeypatch.setattr(network, "run_ps", lambda *a, **k: _ok(_NET_FULL))
    monkeypatch.setattr(
        network, "resolve_netbios_names", lambda ips, **k: {"192.168.1.1": "GATEWAY-PC"}
    )
    res = network.collect_network()
    neighbor = res.payload["network_neighbors"][0]
    assert neighbor["ip"] == "192.168.1.1"
    assert neighbor["name"] == "GATEWAY-PC"
    assert neighbor["name_source"] == "netbios"


def test_collect_network_neighbor_without_resolved_name_has_no_name_keys(monkeypatch):
    monkeypatch.setattr(network, "run_ps", lambda *a, **k: _ok(_NET_FULL))
    monkeypatch.setattr(network, "resolve_netbios_names", lambda ips, **k: {})
    res = network.collect_network()
    neighbor = res.payload["network_neighbors"][0]
    assert "name" not in neighbor
    assert "name_source" not in neighbor


def test_collect_network_resolver_failure_does_not_break_collection(monkeypatch):
    """A resolver crash is best-effort enrichment gone wrong -- must never take
    down the rest of the network collection (adapters/connections/quality)."""
    monkeypatch.setattr(network, "run_ps", lambda *a, **k: _ok(_NET_FULL))

    def boom(ips, **k):
        raise RuntimeError("resolver blew up")

    monkeypatch.setattr(network, "resolve_netbios_names", boom)
    res = network.collect_network()
    assert res.payload is not None
    assert len(res.payload["network_neighbors"]) == 1
    assert "name" not in res.payload["network_neighbors"][0]


def test_collect_network_resolver_receives_only_neighbor_ips(monkeypatch):
    monkeypatch.setattr(network, "run_ps", lambda *a, **k: _ok(_NET_FULL))
    seen = []

    def fake_resolver(ips, **k):
        seen.append(list(ips))
        return {}

    monkeypatch.setattr(network, "resolve_netbios_names", fake_resolver)
    network.collect_network()
    assert seen == [["192.168.1.1"]]  # the multicast neighbor was already dropped


# --------------------------------------------------------------------------- #
# P1: lan_hints wiring (collector internals -> tests/test_lan_discovery.py)   #
# --------------------------------------------------------------------------- #


def test_lan_adapter_ips_keeps_only_lan_and_wifi_role():
    adapters = [
        {"role": "lan", "ipv4": ["10.0.0.5"]},
        {"role": "wifi", "ipv4": ["10.0.0.6"]},
        {"role": "tunnel", "ipv4": ["10.0.85.2"]},  # e.g. an Outline endpoint -- RFC1918 too
        {"role": "virtual", "ipv4": ["10.0.0.7"]},
        {"role": "other", "ipv4": ["10.0.0.8"]},
    ]
    assert network._lan_adapter_ips(adapters) == ["10.0.0.5", "10.0.0.6"]


def test_lan_adapter_ips_skips_empty_ipv4():
    assert network._lan_adapter_ips([{"role": "lan", "ipv4": []}]) == []
    assert network._lan_adapter_ips([{"role": "lan"}]) == []


def test_collect_network_includes_lan_hints_from_lan_adapters(monkeypatch):
    monkeypatch.setattr(network, "run_ps", lambda *a, **k: _ok(_NET_FULL))
    seen = []

    def fake_collect(ips, **k):
        seen.append(list(ips))
        return [{"ip": "192.168.1.9", "source": "mdns", "data_b64": "AAAA"}]

    monkeypatch.setattr(network, "collect_lan_discovery", fake_collect)
    res = network.collect_network()
    assert seen == [["192.168.1.5"]]  # the one "lan"-role adapter's own ipv4
    assert res.payload["lan_hints"] == [{"ip": "192.168.1.9", "source": "mdns", "data_b64": "AAAA"}]


def test_collect_network_lan_discovery_failure_does_not_break_collection(monkeypatch):
    """A multicast-listen failure (blocked port, no permission) is best-effort
    enrichment gone wrong -- must never take down the rest of collection."""
    monkeypatch.setattr(network, "run_ps", lambda *a, **k: _ok(_NET_FULL))

    def boom(ips, **k):
        raise RuntimeError("lan_discovery blew up")

    monkeypatch.setattr(network, "collect_lan_discovery", boom)
    res = network.collect_network()
    assert res.payload is not None
    assert res.payload["lan_hints"] == []
    assert len(res.payload["network_adapters"]) == 1  # rest of the payload is intact


def test_collect_network_caps_lan_hints(monkeypatch):
    monkeypatch.setattr(network, "run_ps", lambda *a, **k: _ok(_NET_FULL))
    oversized = [
        {"ip": f"192.168.1.{i}", "source": "mdns", "data_b64": "AAAA"}
        for i in range(network._MAX_LAN_HINTS + 10)
    ]
    monkeypatch.setattr(network, "collect_lan_discovery", lambda ips, **k: oversized)
    res = network.collect_network()
    assert len(res.payload["lan_hints"]) == network._MAX_LAN_HINTS


def test_collect_network_no_lan_role_adapter_skips_lan_discovery_entirely(monkeypatch):
    """No real LAN/Wi-Fi adapter (e.g. only a tunnel) -> collect_lan_discovery
    must not even be called (nothing to join multicast on)."""
    tunnel_only = {**_NET_FULL, "adapters": [{**_NET_FULL["adapters"][0], "name": "OpenVPN"}]}
    monkeypatch.setattr(network, "run_ps", lambda *a, **k: _ok(tunnel_only))

    def boom(ips, **k):
        raise AssertionError("must not be called with no lan/wifi adapter")

    monkeypatch.setattr(network, "collect_lan_discovery", boom)
    res = network.collect_network()
    assert res.payload["lan_hints"] == []


# --------------------------------------------------------------------------- #
# P2: active-scan wiring (sweep_lan + neighbor rescan; internals ->            #
# tests/test_lan_scan.py). run_ps is faked to distinguish the first pass       #
# (_NET_SCRIPT, contains "Get-NetAdapter") from the rescan                     #
# (_NEIGHBOR_RESCAN_SCRIPT, neighbor-table-only, no "Get-NetAdapter").         #
# --------------------------------------------------------------------------- #
_TUNNEL_ADAPTER = {
    "name": "OpenVPN",
    "desc": "OpenVPN Data Channel Offload",
    "mac": "AA-BB-CC-00-11-33",
    "iftype": 6,
    "up": True,
    "link_bps": 0,
    "ipv4": ["10.0.85.2"],  # RFC1918 too -- must still be excluded via role, not IP
    "ipv6": [],
    "gateway": "",
    "dns": [],
    "dhcp": False,
}


def _first_pass_fake(data, calls=None):
    def fake(script, timeout=30):
        if calls is not None:
            calls.append(script)
        if "Get-NetAdapter" in script:
            return _ok(data)
        return PsResult("empty")  # no rescan configured -- overridden per-test when needed

    return fake


def test_collect_network_active_scan_false_by_default_no_sweep_no_rescan(monkeypatch):
    calls = []
    monkeypatch.setattr(network, "run_ps", _first_pass_fake(_NET_FULL, calls))

    def boom(ips, **k):
        raise AssertionError("sweep_lan must not run when active_scan=False")

    monkeypatch.setattr(network, "sweep_lan", boom)
    res = network.collect_network()  # active_scan defaults to False
    assert res.payload is not None
    assert len(calls) == 1  # only the first-pass script ran -- no rescan call


def test_collect_network_active_scan_sweeps_lan_and_wifi_adapters_only(monkeypatch):
    with_tunnel = {**_NET_FULL, "adapters": [*_NET_FULL["adapters"], _TUNNEL_ADAPTER]}
    monkeypatch.setattr(network, "run_ps", _first_pass_fake(with_tunnel))
    seen = []

    def fake_sweep(ips, **kw):
        seen.append(list(ips))
        return 0

    monkeypatch.setattr(network, "sweep_lan", fake_sweep)
    network.collect_network(active_scan=True)
    assert seen == [["192.168.1.5"]]  # the tunnel adapter's 10.0.85.2 excluded


def test_collect_network_active_scan_merges_new_neighbor_from_rescan(monkeypatch):
    rescan_data = {
        "neighbors": [
            {"ip": "192.168.1.1", "mac": "AA-BB-CC-00-11-22", "state": "Reachable"},
            {"ip": "192.168.1.50", "mac": "AA-BB-CC-00-99-99", "state": "Reachable"},  # new
        ]
    }

    def fake_run_ps(script, timeout=30):
        return _ok(_NET_FULL) if "Get-NetAdapter" in script else _ok(rescan_data)

    monkeypatch.setattr(network, "run_ps", fake_run_ps)
    res = network.collect_network(active_scan=True)
    ips = {n["ip"] for n in res.payload["network_neighbors"]}
    assert ips == {"192.168.1.1", "192.168.1.50"}


def test_collect_network_active_scan_rescan_failure_keeps_first_pass_neighbors(monkeypatch):
    def fake_run_ps(script, timeout=30):
        return _ok(_NET_FULL) if "Get-NetAdapter" in script else PsResult("blocked")

    monkeypatch.setattr(network, "run_ps", fake_run_ps)
    res = network.collect_network(active_scan=True)
    assert [n["ip"] for n in res.payload["network_neighbors"]] == ["192.168.1.1"]


def test_collect_network_active_scan_sweep_raising_rescan_still_runs(monkeypatch):
    rescan_data = {
        "neighbors": [{"ip": "192.168.1.77", "mac": "AA-BB-CC-00-77-77", "state": "Reachable"}]
    }
    calls = []

    def fake_run_ps(script, timeout=30):
        calls.append(script)
        return _ok(_NET_FULL) if "Get-NetAdapter" in script else _ok(rescan_data)

    def boom(ips, **kw):
        raise RuntimeError("sweep blew up")

    monkeypatch.setattr(network, "run_ps", fake_run_ps)
    monkeypatch.setattr(network, "sweep_lan", boom)
    res = network.collect_network(active_scan=True)
    assert len(calls) == 2  # first pass + rescan, despite the sweep raising
    ips = {n["ip"] for n in res.payload["network_neighbors"]}
    assert "192.168.1.77" in ips
