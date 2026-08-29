"""Phase-2 network map: OUI seed, pure builder, subnet anomaly (no DB)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from server.analytics.netmap import (
    build_netmap,
    quality_overlay,
    subnet_anomaly,
    subnet_context_for,
    subnet_hint,
)
from server.analytics.oui import normalize_mac, vendor_for_mac

from tests.conftest import envelope, healthy

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


def test_gateway_extracted_but_arp_only_nodes_are_hidden():
    # The router (a neighbour whose IP is the gateway) is still surfaced in the
    # cluster header, but agentless ARP-only devices are no longer collected or
    # shown on the map (owner 2026-06-22): only agents, gateways and discovered
    # printers belong on it.
    n_unknown = {"ip": "192.168.1.50", "mac": "00:50:56:00:00:09", "state": "Stale"}
    n_gw = {"ip": "192.168.1.1", "mac": "DE-AD-BE-EF-00-01", "state": "Reachable"}
    m = build_netmap(
        [
            _snap("d1", neighbors=[n_unknown, n_gw]),
            _snap("d2", ip="192.168.1.11", mac="AA-BB-CC-00-00-02", neighbors=[n_unknown]),
        ]
    )
    c = m["clusters"][0]
    assert c["others"] == []  # the ARP-only device is not shown
    assert c["gateway_mac"] == "DE-AD-BE-EF-00-01"  # router still shown in the header
    assert m["totals"]["others"] == 0


def test_arp_only_neighbor_seen_by_many_is_still_hidden():
    # However many agents observe an agentless ARP device, it stays off the map.
    n = {"ip": "192.168.1.50", "mac": "00:50:56:00:00:09", "state": "Stale"}
    m = build_netmap(
        [
            _snap("d1", neighbors=[n, dict(n)]),  # duplicate rows in ONE snapshot
            _snap("d2", ip="192.168.1.11", mac="AA-BB-CC-00-00-02", neighbors=[n]),
        ]
    )
    assert m["clusters"][0]["others"] == []


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


# --- Ф2: extracted pure overlay helpers (consumed by netdisco.unified) ---


def test_subnet_hint_quad_or_none():
    assert subnet_hint("192.168.1.50") == "192.168.1.x"
    assert subnet_hint("10.0.0.1") == "10.0.0.x"
    assert subnet_hint("not-an-ip") is None
    assert subnet_hint(None) is None


def test_quality_overlay_reports_loss_latency_or_none():
    snap = _snap("d1", loss=12.0, lat=3.0)
    assert quality_overlay(snap, "192.168.1.1") == {"loss_pct": 12.0, "latency_ms": 3.0}
    assert quality_overlay(snap, "10.9.9.9") is None  # no probe to this gateway
    filtered = _snap(
        "d2",
        quality=[
            {
                "target_kind": "gateway",
                "target": "192.168.1.1",
                "latency_ms": None,
                "loss_pct": 100.0,
            }
        ],
    )
    assert quality_overlay(filtered, "192.168.1.1") is None  # ICMP-ambiguous (D5)


def test_subnet_anomaly_cohort_and_threshold():
    bad = subnet_anomaly([30.0, 40.0])
    assert bad["anomaly"] is True
    assert bad["reporting"] == 2 and bad["degraded"] == 2
    assert bad["loss_pct"] == 35.0  # B6: cohort magnitude, not just the boolean
    assert "инфраструктур" in bad["reason"]
    assert subnet_anomaly([])["loss_pct"] is None
    assert subnet_anomaly([30.0, 0.0, 0.0])["anomaly"] is False
    assert subnet_anomaly([90.0])["anomaly"] is False  # cohort < 2 never alarms
    assert subnet_anomaly([])["anomaly"] is False


# --------------------------------------------------------------------------- #
# o5-B3: get_network_snapshots() reads a narrow net_snapshots projection
# written on ingest, instead of json.loads-ing the full historical payload.
# --------------------------------------------------------------------------- #


def _hist_with_network(**net) -> dict:
    payload = healthy("historical")
    payload.update(net)
    return payload


_NET_FIELDS = {
    "network_adapters": [
        {
            "name": "Ethernet",
            "kind": "ethernet",
            "mac": "AA-BB-CC-00-00-09",
            "up": True,
            "ipv4": ["192.168.1.20"],
            "gateway": "192.168.1.1",
        }
    ],
    "network_neighbors": [{"ip": "192.168.1.1", "mac": "DE-AD-BE-EF-00-01", "state": "Reachable"}],
    "network_quality": [
        {
            "target_kind": "gateway",
            "target": "192.168.1.1",
            "latency_ms": 2.0,
            "loss_pct": 0.0,
            "samples": 3,
        }
    ],
    "network_routes": [{"destination": "0.0.0.0/0", "gateway": "192.168.1.1", "metric": 25}],
    "lan_hints": [{"kind": "mdns", "name": "printer.local", "ip": "192.168.1.30"}],
}


@pytest.mark.integration
def test_store_net_snapshot_roundtrip(client: TestClient) -> None:
    from server import db

    r = client.post(
        "/api/v1/ingest",
        json=envelope("dev-net-1", "historical", _hist_with_network(**_NET_FIELDS)),
    )
    assert r.status_code == 200, r.text
    snaps = {s["device_id"]: s for s in db.get_network_snapshots()}
    assert "dev-net-1" in snaps
    snap = snaps["dev-net-1"]
    assert snap["adapters"] == _NET_FIELDS["network_adapters"]
    assert snap["neighbors"] == _NET_FIELDS["network_neighbors"]
    assert snap["quality"] == _NET_FIELDS["network_quality"]
    assert snap["routes"] == _NET_FIELDS["network_routes"]
    assert snap["lan_hints"] == _NET_FIELDS["lan_hints"]


@pytest.mark.integration
def test_get_network_snapshots_does_not_read_historical(client: TestClient) -> None:
    """The read side must come from the net_snapshots projection: wipe every
    historical row after ingest and confirm get_network_snapshots() still
    answers from the projection, not from a json.loads of the full payload."""
    from server import db

    r = client.post(
        "/api/v1/ingest",
        json=envelope("dev-net-2", "historical", _hist_with_network(**_NET_FIELDS)),
    )
    assert r.status_code == 200, r.text
    with db._connect() as conn:
        conn.execute("DELETE FROM historical WHERE device_id=?", ("dev-net-2",))

    snaps = {s["device_id"]: s for s in db.get_network_snapshots()}
    assert "dev-net-2" in snaps
    assert snaps["dev-net-2"]["adapters"] == _NET_FIELDS["network_adapters"]
