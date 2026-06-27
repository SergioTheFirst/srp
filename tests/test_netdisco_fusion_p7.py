"""Ф7 fusion threading: a ResolvedLink carries the winner's directed port labels,
medium, and VLAN -- with the port labels swapped when endpoints are canonicalised
(a <= b). VLAN is taken from any contributing evidence (LLDP wins priority but does
not report a VLAN; the FDB does). RED first.
"""

from __future__ import annotations

from server.netdisco.evidence import HIGH, SOURCE_FDB_EDGE, SOURCE_LLDP, LinkEvidence
from server.netdisco.fusion import fuse


def test_fuse_threads_ports_medium_vlan_with_canonical_swap():
    # local node "nd-z" (sorts last); remote "nd-mac-aa" (sorts first) -> swap.
    ev = LinkEvidence(
        a="nd-z",
        b="nd-mac-aa",
        source=SOURCE_LLDP,
        confidence=HIGH,
        local_if=3,
        a_port="Gi1/0/3",
        b_port="Gi0/1",
        medium="wired",
        vlan=10,
    )
    links = fuse([ev])
    assert len(links) == 1
    link = links[0]
    assert link.a == "nd-mac-aa" and link.b == "nd-z"  # canonical order
    assert link.a_port == "Gi0/1"  # was b_port, swapped to follow node a
    assert link.b_port == "Gi1/0/3"
    assert link.medium == "wired"
    assert link.vlan == 10


def test_fuse_defaults_medium_wired():
    ev = LinkEvidence(a="nd-a", b="nd-b", source=SOURCE_FDB_EDGE, confidence=HIGH)
    (link,) = fuse([ev])
    assert link.medium == "wired"


def test_fuse_keeps_explicit_wireless_medium():
    ev = LinkEvidence(
        a="nd-mac-cc", b="nd-mac-dd", source="wireless", confidence=HIGH, medium="wireless"
    )
    (link,) = fuse([ev])
    assert link.medium == "wireless"


def test_fuse_takes_vlan_from_any_contributing_evidence():
    # LLDP wins by priority but carries no VLAN; the FDB hint for the same pair does.
    lldp = LinkEvidence(a="nd-a", b="nd-mac-bb", source=SOURCE_LLDP, confidence=HIGH)
    fdb = LinkEvidence(a="nd-a", b="nd-mac-bb", source=SOURCE_FDB_EDGE, confidence=HIGH, vlan=20)
    (link,) = fuse([lldp, fdb])
    assert link.via_source == SOURCE_LLDP  # LLDP still wins
    assert link.vlan == 20  # VLAN preserved from the FDB evidence
