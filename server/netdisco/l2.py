"""Phase 8 -- §4.3 L2 edge inference from the bridge FDB (non-standard algorithm).

LLDP/CDP only see neighbours that *speak* a discovery protocol. The forwarding DB
sees every MAC the switch has learned, so it can place a link to a mute host with no
LLDP at all -- the strong idea naive monitors lack. For each switch port we look at
the set of MACs behind it (multicast/broadcast and the switch's own MACs removed):

* exactly one non-infra MAC  -> a direct EDGE link to that host (HIGH).
* an infra MAC present, or more than ``UPLINK_MAC_THRESHOLD`` MACs -> an UPLINK/TRUNK
  port; only the infra MACs become low-confidence switch<->switch candidates (the
  real peer is resolved later by STP/LLDP in fusion). A trunk with no infra MAC has
  no nameable peer -> emit nothing (UNKNOWN over a fabricated edge).
* two..threshold non-infra MACs -> AMBIGUOUS (a hub/unmanaged switch hangs off the
  port); emit a LOW claim per host rather than one false edge.

Pure function of its inputs; output is sorted so input order never changes the graph
(determinism the fusion/reconcile phases rely on).
"""

from __future__ import annotations

from typing import Dict, FrozenSet, List, Optional, Set

from server.netdisco.evidence import (
    HIGH,
    LOW,
    SOURCE_FDB_AMBIGUOUS,
    SOURCE_FDB_EDGE,
    SOURCE_FDB_UPLINK,
    LinkEvidence,
)

# A trunk/uplink carries many MACs; above this count a port is treated as an uplink
# rather than as many edge links (§4.3 trunk-noise guard).
UPLINK_MAC_THRESHOLD = 4


def _is_multicast(mac: str) -> bool:
    """True for a group/broadcast MAC (least-significant bit of octet 1 set)."""
    try:
        return bool(int(mac[:2], 16) & 1)
    except ValueError:
        return False


def infer_edges(
    local: str,
    port_macs: Dict[int, Set[str]],
    port_ifindex: Dict[int, int],
    *,
    infra_macs: FrozenSet[str] = frozenset(),
    own_macs: FrozenSet[str] = frozenset(),
) -> List[LinkEvidence]:
    """Infer L2 link evidence for switch ``local`` from its FDB (§4.3)."""
    out: List[LinkEvidence] = []
    for port, raw in port_macs.items():
        macs = {m for m in raw if not _is_multicast(m) and m not in own_macs}
        if not macs:
            continue  # empty / clean port
        ifx: Optional[int] = port_ifindex.get(port)
        infra_hits = macs & infra_macs
        if infra_hits or len(macs) > UPLINK_MAC_THRESHOLD:
            # uplink/trunk: name a peer only via known infra MACs; a nameless
            # trunk (no infra MAC) emits nothing -- never a fabricated edge.
            for mac in infra_hits:
                out.append(
                    LinkEvidence(
                        a=local, b=mac, source=SOURCE_FDB_UPLINK, confidence=LOW, local_if=ifx
                    )
                )
        elif len(macs) == 1:
            (mac,) = tuple(macs)
            out.append(
                LinkEvidence(a=local, b=mac, source=SOURCE_FDB_EDGE, confidence=HIGH, local_if=ifx)
            )
        else:  # 2..threshold non-infra MACs -> hub/unmanaged switch behind the port
            for mac in macs:
                out.append(
                    LinkEvidence(
                        a=local, b=mac, source=SOURCE_FDB_AMBIGUOUS, confidence=LOW, local_if=ifx
                    )
                )
    out.sort(key=lambda e: (e.b, e.source, e.local_if if e.local_if is not None else -1))
    return out
