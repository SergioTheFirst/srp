"""Phase 8 -- §4.3 FDB edge inference (RED first).

The non-standard FDB algorithm gives links to mute hosts with no LLDP: for a switch
port, the set of MACs behind it (minus multicast/own) decides the link:

* exactly 1 non-infra MAC  -> EDGE link to that host (HIGH)
* contains an infra MAC, or > UPLINK_MAC_THRESHOLD MACs -> UPLINK/TRUNK; only the
  infra MACs become low-confidence switch<->switch candidates (a nameless trunk
  emits nothing -- UNKNOWN over a fabricated edge)
* 2..threshold non-infra MACs -> AMBIGUOUS (hub/unmanaged switch), LOW per host

Output is sorted so input order never changes the result (property).
"""

from __future__ import annotations

from server.analytics.oui import normalize_mac
from server.netdisco import evidence, oids, reconcile
from server.netdisco.l2 import UPLINK_MAC_THRESHOLD, infer_edges

_HOST_A = normalize_mac("00:1b:44:11:3a:b7")
_HOST_B = normalize_mac("00:1b:44:11:3a:b8")
_INFRA = normalize_mac("aa:bb:cc:dd:ee:ff")
_MCAST = normalize_mac("01:00:5e:00:00:fb")  # first octet odd -> multicast
_BCAST = normalize_mac("ff:ff:ff:ff:ff:ff")
_OWN = normalize_mac("de:ad:be:ef:00:01")


def test_single_non_infra_mac_is_high_edge():
    out = infer_edges("sw1", {3: {_HOST_A}}, {3: 10})
    assert out == [
        evidence.LinkEvidence(
            a="sw1",
            b=_HOST_A,
            source=evidence.SOURCE_FDB_EDGE,
            confidence=evidence.HIGH,
            local_if=10,
        )
    ]


def test_port_with_infra_mac_is_uplink_not_edge():
    # host_a is reachable *through* the uplink to the known infra switch, not
    # directly attached -> only the infra MAC becomes a (low) uplink candidate.
    out = infer_edges("sw1", {3: {_HOST_A, _INFRA}}, {3: 10}, infra_macs={_INFRA})
    assert out == [
        evidence.LinkEvidence(
            a="sw1",
            b=_INFRA,
            source=evidence.SOURCE_FDB_UPLINK,
            confidence=evidence.LOW,
            local_if=10,
        )
    ]


def test_trunk_over_threshold_without_infra_names_no_peer():
    many = {normalize_mac(f"00:00:00:00:00:{i:02x}") for i in range(UPLINK_MAC_THRESHOLD + 1)}
    assert infer_edges("sw1", {3: many}, {3: 10}) == []


def test_two_to_threshold_macs_are_ambiguous_low():
    out = infer_edges("sw1", {3: {_HOST_A, _HOST_B}}, {3: 10})
    assert out == [
        evidence.LinkEvidence(
            a="sw1",
            b=_HOST_A,
            source=evidence.SOURCE_FDB_AMBIGUOUS,
            confidence=evidence.LOW,
            local_if=10,
        ),
        evidence.LinkEvidence(
            a="sw1",
            b=_HOST_B,
            source=evidence.SOURCE_FDB_AMBIGUOUS,
            confidence=evidence.LOW,
            local_if=10,
        ),
    ]


def test_multicast_broadcast_and_own_macs_filtered():
    out = infer_edges("sw1", {3: {_HOST_A, _MCAST, _BCAST, _OWN}}, {3: 10}, own_macs={_OWN})
    assert out == [
        evidence.LinkEvidence(
            a="sw1",
            b=_HOST_A,
            source=evidence.SOURCE_FDB_EDGE,
            confidence=evidence.HIGH,
            local_if=10,
        )
    ]


def test_port_emptied_by_filter_is_skipped():
    assert infer_edges("sw1", {3: {_MCAST, _OWN}}, {3: 10}, own_macs={_OWN}) == []


def test_missing_ifindex_is_none_not_error():
    out = infer_edges("sw1", {3: {_HOST_A}}, {})  # no port->ifindex mapping
    assert out == [
        evidence.LinkEvidence(
            a="sw1",
            b=_HOST_A,
            source=evidence.SOURCE_FDB_EDGE,
            confidence=evidence.HIGH,
            local_if=None,
        )
    ]


def test_fdb_edge_on_radio_port_is_wireless():
    # A7: a port whose ifIndex is one of the device's own radio interfaces (ifType
    # 71) means the FDB edge was learned off an AP's radio -- tag it wireless.
    out = infer_edges("sw1", {5: {_HOST_A}}, {5: 5}, radio_ifindexes=frozenset({5}))
    assert out == [
        evidence.LinkEvidence(
            a="sw1",
            b=_HOST_A,
            source=evidence.SOURCE_FDB_EDGE,
            confidence=evidence.HIGH,
            local_if=5,
            medium="wireless",
        )
    ]
    wired = infer_edges("sw1", {5: {_HOST_A}}, {5: 5}, radio_ifindexes=frozenset())
    assert wired[0].medium is None


def test_output_is_order_independent():
    a = infer_edges(
        "sw1", {3: {_HOST_A, _HOST_B}, 4: {_INFRA}}, {3: 10, 4: 11}, infra_macs={_INFRA}
    )
    b = infer_edges(
        "sw1", {4: {_INFRA}, 3: {_HOST_B, _HOST_A}}, {4: 11, 3: 10}, infra_macs={_INFRA}
    )
    assert a == b
    assert len(a) == 3  # 2 ambiguous (port 3) + 1 uplink (port 4)


# --- o5-A13: infra_macs must not depend only on the classifier's dev_type verdict ---
def test_uplink_port_emits_evidence_when_peer_has_many_interfaces():
    # An unclassified device (dev_type="endpoint") with many stored net_interfaces
    # rows must still seed the FDB-uplink heuristic -- a real switch sitting behind
    # an unconfirmed dev_type must not stay invisible to infra_macs forever.
    peer = normalize_mac("aa:aa:aa:aa:aa:01")
    hosts = {normalize_mac(f"00:00:00:00:01:{i:02x}") for i in range(5)}
    port_macs = hosts | {peer}
    assert len(port_macs) == 6  # > UPLINK_MAC_THRESHOLD

    devices = [{"device_nid": "nd-ep1", "dev_type": "endpoint", "mac": None}]
    # F4: infra_macs now counts only physical ethernet rows (if_type ==
    # IF_TYPE_ETHERNET with a phys_mac), not raw net_interfaces row count -- so
    # this fixture's rows must all carry if_type + a real phys_mac to still
    # seed the heuristic (was: only row 0 had a phys_mac, the rest None).
    interfaces = [
        {
            "device_nid": "nd-ep1",
            "if_index": i,
            "if_type": oids.IF_TYPE_ETHERNET,
            "phys_mac": peer if i == 0 else normalize_mac(f"aa:aa:aa:aa:aa:{0x10 + i:02x}"),
        }
        for i in range(8)  # > _INFRA_IFACE_MIN
    ]
    infra_macs = reconcile._infra_macs(devices, interfaces)
    assert peer in infra_macs  # seeded despite the "endpoint" verdict

    out = infer_edges("sw1", {3: port_macs}, {3: 10}, infra_macs=infra_macs)
    assert out == [
        evidence.LinkEvidence(
            a="sw1",
            b=peer,
            source=evidence.SOURCE_FDB_UPLINK,
            confidence=evidence.LOW,
            local_if=10,
        )
    ]


def test_workstation_with_few_ethernet_rows_among_many_is_not_infra():
    # F4: a stock Windows host routinely has >4 stored net_interfaces rows
    # (loopback, tunnel/6to4, virtual adapters), many carrying a phys_mac too --
    # counting raw row count let an ordinary workstation's MAC leak into
    # infra_macs. Only physical ethernet ports (if_type == IF_TYPE_ETHERNET)
    # may count, and this device has just 2 of those.
    eth_mac = normalize_mac("aa:aa:aa:aa:aa:02")
    interfaces = [
        {
            "device_nid": "nd-ws1",
            "if_index": 0,
            "if_type": oids.IF_TYPE_ETHERNET,
            "phys_mac": eth_mac,
        },
        {
            "device_nid": "nd-ws1",
            "if_index": 1,
            "if_type": oids.IF_TYPE_ETHERNET,
            "phys_mac": normalize_mac("aa:aa:aa:aa:aa:03"),
        },
    ] + [
        {
            "device_nid": "nd-ws1",
            "if_index": i,
            "if_type": 131,  # tunnel/virtual -- not ethernet
            "phys_mac": normalize_mac(f"bb:bb:bb:bb:bb:{i:02x}"),
        }
        for i in range(2, 8)
    ]
    assert len(interfaces) == 8  # > _INFRA_IFACE_MIN by raw row count
    devices = [{"device_nid": "nd-ws1", "dev_type": "endpoint", "mac": None}]
    infra_macs = reconcile._infra_macs(devices, interfaces)
    assert eth_mac not in infra_macs


def test_device_with_six_ethernet_ports_is_infra():
    # Positive control: a device whose physical ethernet port count alone
    # exceeds _INFRA_IFACE_MIN is still seeded as infra.
    macs = [normalize_mac(f"cc:cc:cc:cc:cc:{i:02x}") for i in range(6)]
    interfaces = [
        {"device_nid": "nd-sw2", "if_index": i, "if_type": oids.IF_TYPE_ETHERNET, "phys_mac": m}
        for i, m in enumerate(macs)
    ]
    devices = [{"device_nid": "nd-sw2", "dev_type": "endpoint", "mac": None}]
    infra_macs = reconcile._infra_macs(devices, interfaces)
    assert set(macs) <= infra_macs
