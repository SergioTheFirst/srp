"""Phase 10 -- §1.5/§3.7 reachability correlation: unreachable vs down.

NetXMS's anti-alarm-storm rule, mapped onto the SRP trust core. Before calling a
node DOWN we check the path to it: if that path crosses another down node, the node
is UNREACHABLE (a symptom), its alarms suppressed, and a single root-cause is raised
on the upstream down node. One gateway failure then shows ONE cause, not a hundred
"host down" symptoms.

This is a read-side annotation (like ``subnet_context_for``), not a new alarm engine
(scope ceiling): an incomplete graph caps confidence to a blind-spot, it never
fabricates a failure (D5: UNKNOWN over false confidence). Pure function.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Optional

from server.netdisco.graph import Graph, find_root_cause, path_to_root

DOWN = "down"  # the node itself failed (a root cause)
UNREACHABLE = "unreachable"  # stranded behind a down node (symptom, suppressed)


@dataclass(frozen=True)
class Verdict:
    status: str
    root_cause: Optional[str] = None
    suppressed: bool = False


def _upstream_cause(graph: Graph, node: str, roots: set, causes: set) -> Optional[str]:
    """The first root-cause node on ``node``'s shortest path toward a root."""
    path = path_to_root(graph, node, roots)
    if not path:
        return None
    for hop in path[1:]:  # skip the node itself; walk toward the root
        if hop in causes:
            return hop
    return None


def correlate(graph: Graph, down_set: Iterable[str], roots: Iterable[str]) -> Dict[str, Verdict]:
    """Classify each down node as DOWN (root cause) or UNREACHABLE (suppressed)."""
    downs = set(down_set)
    root_set = set(roots)
    causes = find_root_cause(graph, downs, root_set)
    result: Dict[str, Verdict] = {}
    for node in downs:
        if node in causes:
            result[node] = Verdict(status=DOWN, root_cause=node, suppressed=False)
        else:
            cause = _upstream_cause(graph, node, root_set, causes)
            result[node] = Verdict(status=UNREACHABLE, root_cause=cause, suppressed=True)
    return result
