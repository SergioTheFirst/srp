"""Ф2: unified network-map assembler (pure superset graph, no DB).

One model = netdisco backbone (net_devices/net_links) + Phase-1 identity FKs +
netmap read-side overlays (agent-uplink edges, ICMP quality, subnet anomaly).
Canonical key = device_nid; one physical device -> exactly one node.
"""

from __future__ import annotations

import pytest
from server.netdisco.identity import device_nid
from server.netdisco.unified import build_network_map

pytestmark = pytest.mark.unit

RT, SW, AP = "DE-AD-BE-EF-00-01", "DE-AD-BE-EF-00-02", "DE-AD-BE-EF-00-03"
A1, A2 = "AA-BB-CC-00-00-01", "AA-BB-CC-00-00-02"
PR = "00-11-22-33-44-55"


def _nd(
    nid,
    dev_type="unknown",
    ip=None,
    mac=None,
    vendor=None,
    status="up",
    device_id=None,
    printer_id=None,
    hostname=None,
    model=None,
):
    return {
        "device_nid": nid,
        "dev_type": dev_type,
        "ip": ip,
        "mac": mac,
        "vendor": vendor,
        "status": status,
        "device_id": device_id,
        "printer_id": printer_id,
        "hostname": hostname,
        "model": model,
        "sys_object_id": None,
        "serial": None,
        "site_code": None,
        "first_seen": None,
        "last_seen": None,
    }


def _link(a, b, kind="l2-edge", via="lldp", conf="high", a_if=None, b_if=None):
    return {
        "id": 1,
        "a_nid": a,
        "b_nid": b,
        "link_kind": kind,
        "via_source": via,
        "confidence": conf,
        "a_if": a_if,
        "b_if": b_if,
        "first_seen": None,
        "last_seen": None,
    }


def _snap(
    did, mac, ip="192.168.1.10", gw="192.168.1.1", gw_mac=RT, kind="ethernet", loss=0.0, lat=1.0
):
    neighbors = [{"ip": gw, "mac": gw_mac, "state": "Reachable"}] if gw_mac else []
    return {
        "device_id": did,
        "hostname": f"pc-{did}",
        "adapters": [
            {"name": "n", "kind": kind, "mac": mac, "up": True, "ipv4": [ip], "gateway": gw}
        ],
        "neighbors": neighbors,
        "quality": [{"target_kind": "gateway", "target": gw, "loss_pct": loss, "latency_ms": lat}],
        "last_seen": "2026-06-24T00:00:00+00:00",
    }


def _printer(pid, mac=None, ip="192.168.1.20", vendor="HP", model="LJ", status="online"):
    return {
        "printer_id": pid,
        "ip": ip,
        "hostname": f"prn-{pid}",
        "mac": mac,
        "vendor": vendor,
        "model": model,
        "status": status,
        "total_pages": 10,
        "online": True,
    }


def _by_nid(graph):
    return {n["nid"]: n for n in graph["nodes"]}


def test_superset_covers_both_models_one_node_per_device():
    rt, sw, ap, a1 = (device_nid(mac=m) for m in (RT, SW, AP, A1))
    net_devices = [
        _nd(rt, "router", "192.168.1.1", RT, "Acme"),
        _nd(sw, "switch", "192.168.1.2", SW),
        _nd(ap, "ap", "192.168.1.3", AP),
        _nd(a1, "unknown", "192.168.1.10", A1, device_id="d1", hostname="pc-d1"),
    ]
    snaps = [_snap("d1", A1), _snap("d2", A2, ip="192.168.1.11")]
    g = build_network_map(net_devices, [_link(rt, sw), _link(sw, ap)], snaps, [])
    by = _by_nid(g)
    # backbone nodes (topology) present
    assert rt in by and sw in by and ap in by
    # agent d1 is BOTH a net_device and an agent -> exactly one node
    assert sum(1 for n in g["nodes"] if n.get("device_id") == "d1") == 1
    assert by[a1]["device_id"] == "d1"
    # agent d2 (not discovered) synthesized as its own node
    d2 = device_nid(mac=A2)
    assert d2 in by and by[d2]["device_id"] == "d2"
    # the gateway resolves to the existing router (via ARP mac) -- no duplicate
    assert sum(1 for n in g["nodes"] if n["nid"] == rt) == 1


def test_agent_uplink_edge_carries_quality_and_medium():
    a1, rt = device_nid(mac=A1), device_nid(mac=RT)
    g = build_network_map([], [], [_snap("d1", A1, loss=15.0, lat=4.0)], [])
    uplinks = [e for e in g["links"] if e["link_kind"] == "agent-uplink"]
    assert len(uplinks) == 1
    u = uplinks[0]
    assert {u["a"], u["b"]} == {a1, rt}
    assert u["quality"] == {"loss_pct": 15.0, "latency_ms": 4.0}
    assert u["medium"] == "wired"  # ethernet adapter


def test_wireless_medium_from_wifi_uplink_and_ap_link():
    # wifi agent -> its uplink is wireless
    g = build_network_map([], [], [_snap("d2", A2, kind="wifi", ip="192.168.1.11")], [])
    up = [e for e in g["links"] if e["link_kind"] == "agent-uplink"][0]
    assert up["medium"] == "wireless"
    # a net_link touching an AP node -> wireless (Ф2 heuristic; Ф7 refines to client-assoc)
    sw, ap = device_nid(mac=SW), device_nid(mac=AP)
    g2 = build_network_map(
        [_nd(sw, "switch", mac=SW), _nd(ap, "ap", mac=AP)], [_link(sw, ap)], [], []
    )
    assert g2["links"][0]["medium"] == "wireless"


def test_l3_link_medium():
    a, b = device_nid(mac=RT), device_nid(mac=SW)
    g = build_network_map(
        [_nd(a, "router", mac=RT), _nd(b, "router", mac=SW)],
        [_link(a, b, kind="l3-edge", via="arp", conf="medium")],
        [],
        [],
    )
    assert g["links"][0]["medium"] == "l3"


def test_subnet_anomaly_surfaced_as_field():
    snaps = [
        _snap("d1", A1, ip="192.168.1.10", loss=30.0),
        _snap("d2", A2, ip="192.168.1.11", loss=40.0),
    ]
    g = build_network_map([], [], snaps, [])
    subs = {s["subnet_hint"]: s for s in g["subnets"]}
    assert subs["192.168.1.x"]["anomaly"] is True
    assert "инфраструктур" in subs["192.168.1.x"]["reason"]
    assert g["totals"]["anomalies"] == 1


def test_printer_node_via_fk_no_double():
    pr = device_nid(mac=PR)
    g = build_network_map(
        [_nd(pr, "printer", "192.168.1.20", PR, printer_id="p1", hostname="prn")],
        [],
        [],
        [_printer("p1", mac=PR, ip="192.168.1.20", vendor="HP", model="LJ")],
    )
    matches = [n for n in g["nodes"] if n.get("printer_id") == "p1"]
    assert len(matches) == 1
    n = matches[0]
    assert n["nid"] == pr
    assert n["card_url"] == "/printers/p1"
    assert n["subtype"] == "printer"
    assert n["vendor"] == "HP" and n["model"] == "LJ"


def test_printer_synthesized_when_not_discovered():
    g = build_network_map([], [], [], [_printer("p2", mac=PR, ip="192.168.1.21")])
    pr = device_nid(mac=PR)
    by = _by_nid(g)
    assert pr in by and by[pr]["printer_id"] == "p2"
    assert by[pr]["card_url"] == "/printers/p2"


def test_linked_agent_enriches_sparse_net_device_facts():
    # Ф1 FK may link an already-known net_device row that has only identity. The
    # unified node must still show agent facts already present in the telemetry.
    nid = "nd-chassis-SPARSE-AGENT"
    g = build_network_map(
        [_nd(nid, "unknown", ip=None, mac=None, status=None, device_id="d1")],
        [],
        [_snap("d1", A1, ip="192.168.1.44")],
        [],
    )
    n = _by_nid(g)[nid]
    assert n["dev_type"] == "agent"
    assert n["ip"] == "192.168.1.44"
    assert n["mac"] == "AA-BB-CC-00-00-01"
    assert n["status"] == "up"
    assert n["subnet"] == "192.168.1.x"
    assert n["hostname"] == "pc-d1"


def test_linked_printer_enriches_sparse_net_device_facts():
    nid = "nd-sn-SPARSE-PRINTER"
    g = build_network_map(
        [_nd(nid, "unknown", ip=None, mac=None, status=None, printer_id="p1")],
        [],
        [],
        [_printer("p1", mac=PR, ip="192.168.1.55", vendor="HP", model="LJ", status="idle")],
    )
    n = _by_nid(g)[nid]
    assert n["dev_type"] == "printer"
    assert n["ip"] == "192.168.1.55"
    assert n["mac"] == "00-11-22-33-44-55"
    assert n["vendor"] == "HP"
    assert n["model"] == "LJ"
    assert n["status"] == "idle"
    assert n["subnet"] == "192.168.1.x"


def test_card_url_priority_agent_printer_infra():
    rt, a1, pr = device_nid(mac=RT), device_nid(mac=A1), device_nid(mac=PR)
    net_devices = [
        _nd(rt, "router", "192.168.1.1", RT),
        _nd(a1, "unknown", "192.168.1.10", A1, device_id="d1"),
        _nd(pr, "printer", "192.168.1.20", PR, printer_id="p1"),
    ]
    by = _by_nid(build_network_map(net_devices, [], [], []))
    assert by[rt]["card_url"] == f"/netdisco/device/{rt}"
    assert by[a1]["card_url"] == "/device/d1"
    assert by[pr]["card_url"] == "/printers/p1"


def test_link_endpoints_always_have_nodes():
    a, b = device_nid(mac=RT), device_nid(mac=SW)
    # only 'a' is a known net_device; 'b' is referenced only by the link
    g = build_network_map([_nd(a, "router", mac=RT)], [_link(a, b)], [], [])
    nids = {n["nid"] for n in g["nodes"]}
    assert a in nids and b in nids  # stub node created for the dangling endpoint


def test_agent_node_provenance_and_glyph():
    a1 = device_nid(mac=A1)
    g = build_network_map([], [], [_snap("d1", A1)], [])
    n = _by_nid(g)[a1]
    assert "agent" in n["provenance"]
    assert n["dev_type"] == "agent"


def test_no_identity_devices_skipped_not_collided():
    # neither MAC nor IP -> cannot be keyed by device_nid: skip, never collapse two
    # distinct devices onto a shared nd-unknown node (that silently drops one).
    g = build_network_map(
        [], [], [], [_printer("p1", mac=None, ip=None), _printer("p2", mac=None, ip=None)]
    )
    assert all(n["nid"] != "nd-unknown" for n in g["nodes"])
    assert not [n for n in g["nodes"] if n.get("printer_id") in ("p1", "p2")]
    # an agent whose only adapter has no mac and no ipv4 is likewise unplaceable
    snap = {
        "device_id": "dX",
        "hostname": "x",
        "adapters": [{"kind": "ethernet"}],
        "neighbors": [],
        "quality": [],
        "last_seen": "t",
    }
    g2 = build_network_map([], [], [snap], [])
    assert all(n["nid"] != "nd-unknown" for n in g2["nodes"])


def test_gateway_stub_upgraded_to_router():
    # rt is first seen only as a net_link endpoint (a bare stub), then an agent
    # uplink confirms it is the gateway -> it must end up a router, not "unknown".
    rt, a1 = device_nid(mac=RT), device_nid(mac=A1)
    nds = [_nd(a1, "agent", "192.168.1.10", A1, device_id="d1")]
    g = build_network_map(nds, [_link(a1, rt)], [_snap("d1", A1, gw_mac=RT)], [])
    n = _by_nid(g)[rt]
    assert n["dev_type"] == "router"
    assert n["ip"] == "192.168.1.1"


# --- S1: every node carries its age so the canvas fades stale ghosts (pure: no clock) ---
def test_nodes_carry_freshness_timestamps_so_the_map_self_cleans():
    rt, a1 = device_nid(mac=RT), device_nid(mac=A1)
    nd = _nd(rt, "router", "192.168.1.1", RT)
    nd["first_seen"] = "2026-06-01T00:00:00+00:00"
    nd["last_seen"] = "2026-06-29T12:00:00+00:00"
    g = build_network_map([nd], [], [_snap("d1", A1)], [])
    by = _by_nid(g)
    # discovered infra carries its net_devices timestamps (canvas fades stale ones)
    assert by[rt]["first_seen"] == "2026-06-01T00:00:00+00:00"
    assert by[rt]["last_seen"] == "2026-06-29T12:00:00+00:00"
    # a synthesized agent node carries its live snapshot's last_seen
    assert by[a1]["last_seen"] == "2026-06-24T00:00:00+00:00"


# --- S3: links gain real physics from net_interfaces (speed/alias/oper) -- no new SNMP ---
def test_edge_enriched_with_interface_speed_alias_and_oper_status():
    sw, ap = device_nid(mac=SW), device_nid(mac=AP)
    nds = [_nd(sw, "switch", mac=SW), _nd(ap, "ap", mac=AP)]
    links = [_link(sw, ap, a_if=1, b_if=2)]
    interfaces = [
        {
            "device_nid": sw,
            "if_index": 1,
            "speed_mbps": 1000.0,
            "oper_up": 1,
            "if_alias": "uplink-core",
        },
        {"device_nid": ap, "if_index": 2, "speed_mbps": 1000.0, "oper_up": 0, "if_alias": ""},
    ]
    e = build_network_map(nds, links, [], [], interfaces)["links"][0]
    assert e["speed_mbps"] == 1000.0
    assert e["a_port"] == "uplink-core"  # empty a_port falls back to the operator if_alias
    assert e["port_down"] is True  # the ap-side interface reports oper_up == 0


# --- S4: mark single points of failure on the existing graph (pure topology, no new data) ---
def test_chokepoints_flag_articulation_nodes_and_bridge_links():
    rt, sw, ap = (device_nid(mac=m) for m in (RT, SW, AP))
    nds = [_nd(rt, "router", mac=RT), _nd(sw, "switch", mac=SW), _nd(ap, "ap", mac=AP)]
    g = build_network_map(nds, [_link(rt, sw), _link(sw, ap)], [], [])
    by = _by_nid(g)
    assert by[sw]["articulation"] is True  # the middle of rt-sw-ap is a single point of failure
    assert by[rt]["articulation"] is False and by[ap]["articulation"] is False
    flagged = {frozenset((e["a"], e["b"])) for e in g["links"] if e["bridge"]}
    assert flagged == {frozenset((rt, sw)), frozenset((sw, ap))}


# --- Sprint 2: confirmed badge (B7), change-overlay (S2), flap count (S5) ---
def test_node_confirmed_flag_from_snmp_sysobjectid():
    sw, ap = device_nid(mac=SW), device_nid(mac=AP)
    nd = _nd(sw, "switch", mac=SW)
    nd["sys_object_id"] = "1.3.6.1.4.1.9.1.1"  # SNMP actually answered -> confirmed
    by = _by_nid(build_network_map([nd, _nd(ap, "unknown", mac=AP)], [], [], []))
    assert by[sw]["confirmed"] is True
    assert by[ap]["confirmed"] is False  # no sysObjectID -> a guess, not confirmed


def test_change_overlay_marks_recent_appeared_node_and_added_link():
    rt, sw = device_nid(mac=RT), device_nid(mac=SW)
    nds = [_nd(rt, "router", mac=RT), _nd(sw, "switch", mac=SW)]
    changes = [  # get_net_changes yields newest-first
        {"ts": "2026-06-29T10:00:00+00:00", "device_nid": sw, "kind": "appeared", "detail": {}},
        {
            "ts": "2026-06-29T10:00:00+00:00",
            "device_nid": None,
            "kind": "link_added",
            "detail": {"a": rt, "b": sw},
        },
    ]
    g = build_network_map(nds, [_link(rt, sw)], [], [], net_changes=changes)
    by = _by_nid(g)
    assert by[sw]["change"] == "appeared" and by[sw]["change_ts"] == "2026-06-29T10:00:00+00:00"
    assert by[rt]["change"] is None
    assert g["links"][0]["change"] == "link_added"


def test_reachability_series_and_flap_count_on_node():
    a1 = device_nid(mac=A1)
    series = {a1: ["up", "up", "down", "up", "down"]}  # 3 up<->down transitions
    by = _by_nid(
        build_network_map(
            [_nd(a1, "agent", mac=A1, device_id="d1")], [], [], [], status_series=series
        )
    )
    assert by[a1]["reach_series"] == ["up", "up", "down", "up", "down"]
    assert by[a1]["flaps"] == 3
