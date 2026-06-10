"""Phase-2 network map: OUI seed, pure builder, subnet anomaly (no DB)."""

from __future__ import annotations

import pytest
from server.analytics.netmap import build_netmap, subnet_context_for
from server.analytics.oui import normalize_mac, vendor_for_mac

pytestmark = pytest.mark.unit


def test_normalize_mac_forms():
    assert normalize_mac("00:50:56:aa:bb:cc") == "00-50-56-AA-BB-CC"
    assert normalize_mac("0050.56aa.bbcc") == "00-50-56-AA-BB-CC"
    assert normalize_mac("00-50-56-AA-BB-CC") == "00-50-56-AA-BB-CC"
    assert normalize_mac("garbage") is None
    assert normalize_mac("") is None
    assert normalize_mac(None) is None


def test_vendor_seed_hit_and_honest_unknown():
    assert vendor_for_mac("00:50:56:01:02:03") == "VMware"
    assert vendor_for_mac("B8-27-EB-99-88-77") == "Raspberry Pi"
    assert vendor_for_mac("F4-39-09-11-22-33") is None  # unknown OUI -> no invented vendor
    assert vendor_for_mac(None) is None


def _snap(
    did,
    gw="192.168.1.1",
    ip="192.168.1.10",
    mac="AA-BB-CC-00-00-01",
    loss=0.0,
    lat=1.0,
    neighbors=None,
    quality=None,
    adapters=None,
):
    if adapters is None:
        adapters = [
            {
                "name": "Ethernet",
                "kind": "ethernet",
                "mac": mac,
                "up": True,
                "ipv4": [ip],
                "gateway": gw,
            }
        ]
    if quality is None:
        quality = [
            {
                "target_kind": "gateway",
                "target": gw,
                "latency_ms": lat,
                "loss_pct": loss,
                "samples": 3,
            }
        ]
    return {
        "device_id": did,
        "hostname": f"pc-{did}",
        "site_code": None,
        "site_name": None,
        "last_seen": "2026-06-10T00:00:00+00:00",
        "adapters": adapters,
        "neighbors": neighbors or [],
        "quality": quality,
    }


def test_same_gateway_one_cluster_agents_merged_by_mac():
    s1 = _snap(
        "d1",
        mac="AA-BB-CC-00-00-01",
        neighbors=[{"ip": "192.168.1.11", "mac": "aa:bb:cc:00:00:02", "state": "Reachable"}],
    )
    s2 = _snap("d2", ip="192.168.1.11", mac="AA-BB-CC-00-00-02")
    m = build_netmap([s1, s2])
    assert m["totals"]["clusters"] == 1
    c = m["clusters"][0]
    assert c["gateway"] == "192.168.1.1"
    assert c["subnet_hint"] == "192.168.1.x"
    assert {a["device_id"] for a in c["agents"]} == {"d1", "d2"}
    assert c["others"] == []  # d2's MAC matched an agent -> never an "unknown device"


def test_unknown_neighbor_union_dedup_and_gateway_extraction():
    n_unknown = {"ip": "192.168.1.50", "mac": "00:50:56:00:00:09", "state": "Stale"}
    n_gw = {"ip": "192.168.1.1", "mac": "DE-AD-BE-EF-00-01", "state": "Reachable"}
    m = build_netmap(
        [
            _snap("d1", neighbors=[n_unknown, n_gw]),
            _snap("d2", ip="192.168.1.11", mac="AA-BB-CC-00-00-02", neighbors=[n_unknown]),
        ]
    )
    c = m["clusters"][0]
    assert len(c["others"]) == 1
    other = c["others"][0]
    assert other["seen_by"] == 2 and other["vendor"] == "VMware"
    assert c["gateway_mac"] == "DE-AD-BE-EF-00-01"  # router shown in header, not others
    assert m["totals"]["others"] == 1


def test_subnet_anomaly_threshold():
    bad = build_netmap(
        [
            _snap("d1", loss=30.0),
            _snap("d2", ip="192.168.1.11", mac="AA-BB-CC-00-00-02", loss=40.0),
        ]
    )
    assert bad["clusters"][0]["anomaly"] is True
    assert "инфраструктур" in bad["clusters"][0]["anomaly_reason"]
    ok = build_netmap(
        [
            _snap("d1", loss=30.0),
            _snap("d2", ip="192.168.1.11", mac="AA-BB-CC-00-00-02", loss=0.0),
            _snap("d3", ip="192.168.1.12", mac="AA-BB-CC-00-00-03", loss=0.0),
        ]
    )
    assert ok["clusters"][0]["anomaly"] is False
    single = build_netmap([_snap("d1", loss=90.0)])
    assert single["clusters"][0]["anomaly"] is False  # cohort < 2 never alarms


def test_icmp_filtered_device_not_counted_as_reporting():
    filtered = _snap(
        "d1",
        quality=[
            {
                "target_kind": "gateway",
                "target": "192.168.1.1",
                "latency_ms": None,
                "loss_pct": 100.0,
                "samples": 3,
            }
        ],
    )
    m = build_netmap([filtered, _snap("d2", ip="192.168.1.11", mac="AA-BB-CC-00-00-02", loss=25.0)])
    q = m["clusters"][0]["quality"]
    assert q["reporting"] == 1 and q["degraded"] == 1
    assert m["clusters"][0]["anomaly"] is False  # 1 reporting < min cohort


def test_no_gateway_goes_unclustered_and_context_annotation():
    nogw = _snap(
        "d3",
        adapters=[
            {
                "name": "eth",
                "kind": "ethernet",
                "mac": "AA-BB-CC-00-00-03",
                "up": True,
                "ipv4": ["10.0.0.5"],
                "gateway": None,
            }
        ],
        quality=[],
    )
    snaps = [
        _snap("d1", loss=30.0),
        _snap("d2", ip="192.168.1.11", mac="AA-BB-CC-00-00-02", loss=40.0),
        nogw,
    ]
    m = build_netmap(snaps)
    assert [u["device_id"] for u in m["unclustered"]] == ["d3"]
    note = subnet_context_for(snaps, "d1")
    assert note is not None and "192.168.1.x" in note
    assert subnet_context_for(snaps, "d3") is None
