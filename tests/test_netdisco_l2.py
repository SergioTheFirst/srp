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
from server.netdisco import evidence
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


def test_output_is_order_independent():
    a = infer_edges(
        "sw1", {3: {_HOST_A, _HOST_B}, 4: {_INFRA}}, {3: 10, 4: 11}, infra_macs={_INFRA}
    )
    b = infer_edges(
        "sw1", {4: {_INFRA}, 3: {_HOST_B, _HOST_A}}, {4: 11, 3: 10}, infra_macs={_INFRA}
    )
    assert a == b
    assert len(a) == 3  # 2 ambiguous (port 3) + 1 uplink (port 4)
