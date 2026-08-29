"""Ф7 read-side: the unified map consumes the new persisted link attributes
(stored ``medium``/``vlan``, directed ``a_port``/``b_port``) and the node
``subtype`` (LLDP-MED phone/AP). The stored medium wins over the Ф2 AP heuristic;
the heuristic remains the fallback for pre-Ф7 links with no stored medium. RED first.
"""

from __future__ import annotations

from server.netdisco.identity import device_nid
from server.netdisco.unified import build_network_map


def _dev(nid, **kw):
    base = {"device_nid": nid, "dev_type": "endpoint", "ip": None, "mac": None}
    base.update(kw)
    return base


def test_real_link_uses_stored_medium_vlan_and_ports():
    devs = [_dev("nd-mac-aa"), _dev("nd-mac-bb")]
    links = [
        {
            "a_nid": "nd-mac-aa",
            "b_nid": "nd-mac-bb",
            "link_kind": "l2-edge",
            "via_source": "lldp",
            "confidence": "high",
            "medium": "wireless",
            "vlan": 42,
            "a_port": "Gi0/1",
            "b_port": "Gi0/2",
        }
    ]
    graph = build_network_map(devs, links, [], [])
    edge = next(e for e in graph["links"] if e["link_kind"] == "l2-edge")
    assert edge["medium"] == "wireless"
    assert edge["vlan"] == 42
    assert edge["a_port"] == "Gi0/1"
    assert edge["b_port"] == "Gi0/2"


def test_real_link_falls_back_to_heuristic_when_medium_absent():
    # No stored medium + an AP endpoint -> the Ф2 heuristic still yields "wireless".
    devs = [_dev("nd-mac-aa", dev_type="ap"), _dev("nd-mac-bb")]
    links = [
        {
            "a_nid": "nd-mac-aa",
            "b_nid": "nd-mac-bb",
            "link_kind": "l2-edge",
            "via_source": "fdb_edge",
            "confidence": "high",
        }
    ]
    graph = build_network_map(devs, links, [], [])
    edge = next(e for e in graph["links"] if e["link_kind"] == "l2-edge")
    assert edge["medium"] == "wireless"
    assert edge["vlan"] is None


def test_link_physics_resolves_speed_from_interfaces():
    # o5-A10 (e): a_if now survives fuse -> _link_row -> net_links; once it's on the
    # persisted row, _link_physics must resolve negotiated speed off net_interfaces
    # by ifIndex (previously always None -- a_if never reached net_links).
    devs = [_dev("nd-mac-aa"), _dev("nd-mac-bb")]
    links = [
        {
            "a_nid": "nd-mac-aa",
            "b_nid": "nd-mac-bb",
            "link_kind": "l2-edge",
            "via_source": "fdb_edge",
            "confidence": "high",
            "a_if": 7,
        }
    ]
    net_interfaces = [{"device_nid": "nd-mac-aa", "if_index": 7, "speed_mbps": 1000}]
    graph = build_network_map(devs, links, [], [], net_interfaces)
    edge = next(e for e in graph["links"] if e["link_kind"] == "l2-edge")
    assert edge["speed_mbps"] == 1000


def test_node_carries_stored_subtype():
    devs = [_dev("nd-mac-aa", subtype="phone")]
    graph = build_network_map(devs, [], [], [])
    node = next(n for n in graph["nodes"] if n["nid"] == "nd-mac-aa")
    assert node["subtype"] == "phone"


def test_printer_subtype_still_wins_over_stored():
    # A printer FK link must keep the "printer" subtype regardless of a stale stored one.
    devs = [_dev("nd-mac-aa", subtype="phone", printer_id="p1")]
    graph = build_network_map(devs, [], [], [])
    node = next(n for n in graph["nodes"] if n["nid"] == "nd-mac-aa")
    assert node["subtype"] == "printer"


def test_totals_count_wireless_from_stored_medium():
    devs = [_dev("nd-mac-aa"), _dev("nd-mac-bb")]
    links = [
        {
            "a_nid": "nd-mac-aa",
            "b_nid": "nd-mac-bb",
            "link_kind": "l2-edge",
            "via_source": "wireless",
            "confidence": "high",
            "medium": "wireless",
        }
    ]
    graph = build_network_map(devs, links, [], [])
    assert graph["totals"]["wireless_links"] == 1


def _gw_snap(gw="192.168.1.1"):
    return {
        "device_id": "d1",
        "hostname": "pc-d1",
        "adapters": [
            {
                "name": "n",
                "kind": "ethernet",
                "mac": "BB-BB-BB-BB-BB-BB",
                "up": True,
                "ipv4": ["192.168.1.10"],
                "gateway": gw,
            }
        ],
        "neighbors": [],
        "quality": [],
        "last_seen": None,
    }


def test_gateway_endpoint_row_is_upgraded_to_router():
    # o5-A3: an ARP-derived "endpoint" stub for the LAN gateway must be upgraded once
    # an agent's own route table names it as next-hop -- being a next-hop is strictly
    # stronger evidence than a passive ARP guess, so the bare stub is repainted.
    gw_nid = "nd-mac-AA-AA-AA-AA-AA-AA"
    devs = [_dev(gw_nid, ip="192.168.1.1")]
    graph = build_network_map(devs, [], [_gw_snap()], [])
    node = next(n for n in graph["nodes"] if n["nid"] == gw_nid)
    assert node["dev_type"] == "router"
    assert "gateway" in node["provenance"]


def test_gateway_does_not_override_switch():
    # Companion guard for o5-A3: an SNMP-classified switch must never be repainted
    # "router" just because an agent's default gateway happens to point at it.
    gw_nid = "nd-mac-AA-AA-AA-AA-AA-AA"
    devs = [_dev(gw_nid, dev_type="switch", ip="192.168.1.1")]
    graph = build_network_map(devs, [], [_gw_snap()], [])
    node = next(n for n in graph["nodes"] if n["nid"] == gw_nid)
    assert node["dev_type"] == "switch"


def test_unknown_medium_is_not_claimed_wired():
    # o5-A4: no fail-open -- an FDB link between two plain endpoints (no stored
    # medium, no AP involved, not an LLDP/CDP wired-discovery source) must render as
    # "unknown", never a confidently wrong "wired".
    devs = [_dev("nd-mac-aa"), _dev("nd-mac-bb")]
    links = [
        {
            "a_nid": "nd-mac-aa",
            "b_nid": "nd-mac-bb",
            "link_kind": "l2-edge",
            "via_source": "fdb_edge",
            "confidence": "high",
        }
    ]
    graph = build_network_map(devs, links, [], [])
    edge = next(e for e in graph["links"] if e["link_kind"] == "l2-edge")
    assert edge["medium"] == "unknown"


def _agent_snap(device_id, mac, gw, adapter_extra):
    adapter = {
        "name": "n",
        "mac": mac,
        "up": True,
        "ipv4": [f"{gw.rsplit('.', 1)[0]}.50"],
        "gateway": gw,
    }
    adapter.update(adapter_extra)
    return {
        "device_id": device_id,
        "hostname": f"pc-{device_id}",
        "adapters": [adapter],
        "neighbors": [],
        "quality": [],
    }


def _uplink_medium(graph, agent_nid):
    edge = next(
        e
        for e in graph["links"]
        if e["link_kind"] == "agent-uplink" and agent_nid in (e["a"], e["b"])
    )
    return edge["medium"]


def test_agent_uplink_medium_from_phys_medium():
    # o5-A5: NdisPhysicalMedium (numeric, language-independent) overrides a
    # textual `kind` that a Wi-Fi driver mislabels "ethernet"; a tunnel adapter
    # is always "l3" regardless of medium.
    mac_w, mac_u, mac_t = "AA-AA-AA-AA-AA-01", "AA-AA-AA-AA-AA-02", "AA-AA-AA-AA-AA-03"
    snaps = [
        _agent_snap("d-w", mac_w, "192.168.1.1", {"kind": "ethernet", "phys_medium": 9}),
        _agent_snap("d-u", mac_u, "192.168.2.1", {"kind": None, "phys_medium": None}),
        _agent_snap("d-t", mac_t, "192.168.3.1", {"role": "tunnel", "tunnel": True}),
    ]
    graph = build_network_map([], [], snaps, [])
    assert _uplink_medium(graph, device_nid(mac=mac_w)) == "wireless"
    assert _uplink_medium(graph, device_nid(mac=mac_u)) == "unknown"
    assert _uplink_medium(graph, device_nid(mac=mac_t)) == "l3"
