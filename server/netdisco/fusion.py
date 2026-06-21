"""Phase 9 -- §4.4 data fusion: reconcile competing link evidence into one graph.

A physical link is the *winner* among competing pieces of evidence, never a bare
fact. We normalise each evidence endpoint to a stable node-id (a MAC ->
``nd-mac-...``; an LLDP/CDP device-id -> ``nd-chassis-...``; an already-formed nid
passes through), group evidence by the unordered node pair, and pick the winner by
``SOURCE_PRIORITY`` then freshness (LLDP > CDP > FDB-edge > FDB-uplink > FDB-ambiguous).

The same chassis-id always maps to the same node, so a neighbour seen under several
rem-indexes collapses to one node (node-merge by chassis-id). When a single switch
port resolves to two different neighbours at the *same* source, that is a topology
contradiction: both links are kept but marked LOW + ambiguous (UNKNOWN over a
fabricated single edge). Output is sorted by (a, b, source) so the same evidence in
any order yields a byte-identical graph -- the determinism reconcile relies on.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Dict, FrozenSet, List, Optional, Tuple

from server.analytics.oui import normalize_mac
from server.netdisco import identity
from server.netdisco.evidence import (
    LOW,
    SOURCE_CDP,
    SOURCE_FDB_AMBIGUOUS,
    SOURCE_FDB_EDGE,
    SOURCE_FDB_UPLINK,
    SOURCE_LLDP,
    LinkEvidence,
)
from server.netdisco.models import ResolvedLink

# Source authority (RFC §4.4): a standards-based neighbour beats a vendor one beats
# an FDB inference; an uplink/ambiguous FDB hint is weakest. Unknown source -> 0.
SOURCE_PRIORITY: Dict[str, int] = {
    SOURCE_LLDP: 5,
    SOURCE_CDP: 4,
    SOURCE_FDB_EDGE: 3,
    "route": 3,
    "arp": 2,
    SOURCE_FDB_UPLINK: 2,
    SOURCE_FDB_AMBIGUOUS: 1,
}

_TRUNK_KIND = "l2-trunk"
_EDGE_KIND = "l2-edge"


def _node_id(hint: str) -> str:
    """Evidence endpoint hint -> stable node-id (passthrough for an existing nid)."""
    if hint.startswith("nd-"):
        return hint
    mac = normalize_mac(hint)
    if mac:
        return "nd-mac-" + mac
    return identity.device_nid(chassis_id=hint)


def _priority(source: str) -> int:
    return SOURCE_PRIORITY.get(source, 0)


def _link_kind(source: str) -> str:
    return _TRUNK_KIND if source == SOURCE_FDB_UPLINK else _EDGE_KIND


def _conflicted_ports(evidence: List[LinkEvidence]) -> set:
    """Ports (local node, ifIndex) whose top-priority source names >1 remote node.

    That is a physical contradiction -- one port cannot be a direct edge to two
    different hosts -- so every link off such a port is later flagged ambiguous."""
    by_port: Dict[Tuple[str, int], List[LinkEvidence]] = defaultdict(list)
    for ev in evidence:
        node_a, node_b = _node_id(ev.a), _node_id(ev.b)
        if ev.local_if is not None and node_a != node_b:
            by_port[(node_a, ev.local_if)].append(ev)
    conflicted = set()
    for key, evs in by_port.items():
        top = max(_priority(e.source) for e in evs)
        remotes = {_node_id(e.b) for e in evs if _priority(e.source) == top}
        if len(remotes) > 1:
            conflicted.add(key)
    return conflicted


def fuse(evidence: List[LinkEvidence]) -> List[ResolvedLink]:
    """Resolve competing :class:`LinkEvidence` into a deterministic link list."""
    conflicted = _conflicted_ports(evidence)
    groups: Dict[FrozenSet[str], List[LinkEvidence]] = defaultdict(list)
    for ev in evidence:
        node_a, node_b = _node_id(ev.a), _node_id(ev.b)
        if node_a == node_b:
            continue  # self-link (own MAC seen on own port) -- never an edge
        groups[frozenset((node_a, node_b))].append(ev)

    out: List[ResolvedLink] = []
    for pair, evs in groups.items():
        winner = max(evs, key=lambda e: (_priority(e.source), e.observed_at or "", e.source))
        node_a, node_b = sorted(pair)
        ambiguous = any(
            e.local_if is not None and (_node_id(e.a), e.local_if) in conflicted for e in evs
        )
        confidence = LOW if ambiguous else winner.confidence
        observed_at: Optional[str] = max(
            (e.observed_at for e in evs if e.observed_at), default=None
        )
        out.append(
            ResolvedLink(
                a=node_a,
                b=node_b,
                via_source=winner.source,
                confidence=confidence,
                link_kind=_link_kind(winner.source),
                ambiguous=ambiguous,
                observed_at=observed_at,
            )
        )
    out.sort(key=lambda link: (link.a, link.b, link.via_source))
    return out
