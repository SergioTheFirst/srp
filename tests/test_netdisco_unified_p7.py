"""Ф7 read-side: the unified map consumes the new persisted link attributes
(stored ``medium``/``vlan``, directed ``a_port``/``b_port``) and the node
``subtype`` (LLDP-MED phone/AP). The stored medium wins over the Ф2 AP heuristic;
the heuristic remains the fallback for pre-Ф7 links with no stored medium. RED first.
"""

from __future__ import annotations

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
