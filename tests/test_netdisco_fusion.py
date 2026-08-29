"""Phase 9 -- §4.4 data fusion: competing evidence -> one resolved graph (RED first).

Evidence endpoints are normalised to stable node-ids (a MAC -> nd-mac-..., an
LLDP/CDP device-id -> nd-chassis-...); evidence is grouped by the unordered node
pair and a winner is chosen by SOURCE_PRIORITY then freshness (LLDP > CDP > FDB).
A single port that resolves to two different neighbours at the same source is a
topology conflict -> LOW + ambiguous (show both, never fabricate one). Output is
sorted so the same evidence in any order yields an identical graph.
"""

from __future__ import annotations

from server.analytics.oui import normalize_mac
from server.netdisco import evidence, fusion
from server.netdisco.models import ResolvedLink

SW1 = "nd-chassis-SW1"
SW2 = "nd-chassis-SW2"
HOSTMAC = normalize_mac("00:11:22:33:44:55")
MAC_A = normalize_mac("00:00:00:00:00:0a")
MAC_B = normalize_mac("00:00:00:00:00:0b")


def _ev(a, b, source, conf, local_if=None, observed_at=None, port_space="ifindex"):
    return evidence.LinkEvidence(a, b, source, conf, local_if, observed_at, port_space=port_space)


def test_lldp_beats_fdb_for_the_same_pair():
    out = fusion.fuse(
        [
            _ev(SW1, HOSTMAC, evidence.SOURCE_FDB_EDGE, evidence.HIGH, 3),
            _ev(SW1, HOSTMAC, evidence.SOURCE_LLDP, evidence.HIGH, 3),
        ]
    )
    assert len(out) == 1
    link = out[0]
    assert isinstance(link, ResolvedLink)
    assert link.via_source == evidence.SOURCE_LLDP
    assert {link.a, link.b} == {SW1, "nd-mac-" + HOSTMAC}
    assert link.confidence == evidence.HIGH
    assert link.ambiguous is False


def test_single_fdb_edge_resolves_to_one_link():
    out = fusion.fuse([_ev(SW1, HOSTMAC, evidence.SOURCE_FDB_EDGE, evidence.HIGH, 3)])
    assert len(out) == 1
    assert out[0].via_source == evidence.SOURCE_FDB_EDGE
    assert out[0].link_kind == "l2-edge"


def test_same_chassis_id_merges_to_one_node_one_link():
    out = fusion.fuse(
        [
            _ev(SW1, "Switch2", evidence.SOURCE_LLDP, evidence.HIGH, 3),
            _ev(SW1, "Switch2", evidence.SOURCE_LLDP, evidence.HIGH, 3),
        ]
    )
    assert len(out) == 1
    assert {out[0].a, out[0].b} == {SW1, "nd-chassis-SWITCH2"}


def test_output_is_deterministic_regardless_of_input_order():
    e1 = _ev(SW1, MAC_A, evidence.SOURCE_FDB_EDGE, evidence.HIGH, 3)
    e2 = _ev(SW1, MAC_B, evidence.SOURCE_FDB_EDGE, evidence.HIGH, 4)
    e3 = _ev(SW2, MAC_A, evidence.SOURCE_LLDP, evidence.HIGH, 1)
    assert fusion.fuse([e1, e2, e3]) == fusion.fuse([e3, e1, e2])
    assert len(fusion.fuse([e1, e2, e3])) == 3


def test_one_port_two_equal_source_neighbours_is_ambiguous_low():
    # same switch port 3 learns two different edge MACs at the same source -> a
    # physical contradiction; both links are kept but flagged LOW + ambiguous.
    out = fusion.fuse(
        [
            _ev(SW1, MAC_A, evidence.SOURCE_FDB_EDGE, evidence.HIGH, 3),
            _ev(SW1, MAC_B, evidence.SOURCE_FDB_EDGE, evidence.HIGH, 3),
        ]
    )
    assert len(out) == 2
    assert all(link.ambiguous and link.confidence == evidence.LOW for link in out)


def test_winner_tiebreak_deterministic_across_equal_priority_sources():
    # "route" and "fdb_edge" share priority 3; with equal freshness the winner must
    # not depend on input order (forward-proofing for route/arp evidence in P10+).
    e_route = _ev(SW1, HOSTMAC, "route", evidence.HIGH, 3)
    e_fdb = _ev(SW1, HOSTMAC, evidence.SOURCE_FDB_EDGE, evidence.HIGH, 3)
    assert (
        fusion.fuse([e_route, e_fdb])[0].via_source == fusion.fuse([e_fdb, e_route])[0].via_source
    )


def test_empty_evidence_yields_empty_graph():
    assert fusion.fuse([]) == []


def test_self_link_is_dropped():
    assert fusion.fuse([_ev(SW1, SW1, evidence.SOURCE_LLDP, evidence.HIGH, 3)]) == []


def test_lldp_port_number_does_not_conflict_with_fdb_ifindex():
    # o5-A9: LLDP's local_if is a bridge-port number; FDB's local_if is an ifIndex --
    # two different numbering spaces that can coincidentally share the same digit.
    # A genuine LLDP<->LLDP conflict on bridge port 1 (SW1 reports two neighbours on
    # the same port -- a real contradiction) must not spill over and mark an
    # unrelated FDB edge that happens to sit on ifIndex 1 as ambiguous too.
    out = fusion.fuse(
        [
            _ev(SW1, "X", evidence.SOURCE_LLDP, evidence.HIGH, 1, port_space="lldp"),
            _ev(SW1, "Z", evidence.SOURCE_LLDP, evidence.HIGH, 1, port_space="lldp"),
            _ev(SW1, MAC_A, evidence.SOURCE_FDB_EDGE, evidence.HIGH, 1),
        ]
    )
    fdb_link = next(link for link in out if link.via_source == evidence.SOURCE_FDB_EDGE)
    assert fdb_link.ambiguous is False
    lldp_links = [link for link in out if link.via_source == evidence.SOURCE_LLDP]
    assert len(lldp_links) == 2
    assert all(link.ambiguous for link in lldp_links)  # the real conflict still shows


def test_lldp_local_if_is_not_carried_as_a_if():
    # F2: lldpRemLocalPortNum (port_space="lldp") is a bridge-port number, not an
    # ifIndex -- writing it into a_if would make unified._link_physics join
    # net_interfaces by the wrong numbering space (chasing a_if via A9's guard,
    # not producing it). Non-ifindex port_space must resolve to a_if=None.
    out = fusion.fuse(
        [_ev(SW1, "Neighbour", evidence.SOURCE_LLDP, evidence.HIGH, 1, port_space="lldp")]
    )
    assert len(out) == 1
    assert out[0].a_if is None
    assert out[0].b_if is None


def test_fdb_local_if_still_carried_as_a_if():
    # Default port_space ("ifindex") is unaffected by the F2 guard.
    out = fusion.fuse([_ev(SW1, HOSTMAC, evidence.SOURCE_FDB_EDGE, evidence.HIGH, 7)])
    assert len(out) == 1
    assert out[0].a_if == 7


def test_fdb_edge_medium_stays_none():
    # o5-A4: no fail-open to "wired" -- an FDB edge with no reported medium must stay
    # unknown (None) at the fusion layer; UNKNOWN over false confidence.
    out = fusion.fuse([_ev(SW1, HOSTMAC, evidence.SOURCE_FDB_EDGE, evidence.HIGH, 3)])
    assert len(out) == 1
    assert out[0].medium is None
