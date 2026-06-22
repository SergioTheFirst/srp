"""Phase 10 -- §3.5 graph engine: pure operations over the topology graph.

An in-memory undirected adjacency built from ``net_devices`` + ``net_links``. BFS is
O(V+E) and a LAN graph is small, so every call rebuilds nothing it does not need.
The engine is deliberately pure (no DB, no clock): correlation (§3.7) and the
reachability poll feed it a device list, a link list, and a ``down_set``, and get
back reachability / paths / root causes -- deterministic and trivially testable.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Optional, Set


@dataclass(frozen=True)
class Graph:
    """Undirected adjacency: ``{node -> frozenset(neighbours)}``. Every known node
    is a key (an isolated device maps to an empty set)."""

    adjacency: Dict[str, FrozenSet[str]]


def build_graph(devices: Iterable[dict], links: Iterable[dict]) -> Graph:
    """Build the undirected graph from device rows + resolved link rows (pure)."""
    adj: Dict[str, Set[str]] = {}
    for dev in devices:
        nid = dev.get("device_nid")
        if nid:
            adj.setdefault(nid, set())
    for link in links:
        a, b = link.get("a_nid"), link.get("b_nid")
        if not a or not b or a == b:
            continue
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)
    return Graph(adjacency={node: frozenset(nbrs) for node, nbrs in adj.items()})


def neighbors(graph: Graph, node: str) -> FrozenSet[str]:
    return graph.adjacency.get(node, frozenset())


def reachable_from(
    graph: Graph, roots: Iterable[str], *, blocked: FrozenSet[str] = frozenset()
) -> Set[str]:
    """Every node reachable from a root without traversing a ``blocked`` node.

    A blocked (down) node is neither entered nor counted -- so the set is exactly
    the up-connected component(s) of the roots."""
    seen: Set[str] = set()
    queue: deque = deque()
    for root in roots:
        if root in graph.adjacency and root not in blocked and root not in seen:
            seen.add(root)
            queue.append(root)
    while queue:
        node = queue.popleft()
        for nbr in graph.adjacency.get(node, frozenset()):
            if nbr not in seen and nbr not in blocked:
                seen.add(nbr)
                queue.append(nbr)
    return seen


def path_to_root(graph: Graph, node: str, roots: Iterable[str]) -> Optional[List[str]]:
    """Shortest hop chain ``[node, ..., root]`` to any root, or None if none exists."""
    root_set = set(roots)
    if node in root_set:
        return [node]
    prev: Dict[str, Optional[str]] = {node: None}
    queue: deque = deque([node])
    while queue:
        cur = queue.popleft()
        if cur in root_set:
            chain = []
            step: Optional[str] = cur
            while step is not None:
                chain.append(step)
                step = prev[step]
            chain.reverse()  # was [root..node]; want [node..root]
            return chain
        for nbr in sorted(graph.adjacency.get(cur, frozenset())):  # sorted -> deterministic
            if nbr not in prev:
                prev[nbr] = cur
                queue.append(nbr)
    return None


def find_root_cause(graph: Graph, down_set: Iterable[str], roots: Iterable[str]) -> Set[str]:
    """The topmost down nodes: those a root can still reach without crossing another
    down node (a root itself, or a node adjacent to the up-reachable component)."""
    downs = set(down_set)
    root_set = set(roots)
    up_reachable = reachable_from(graph, root_set, blocked=frozenset(downs))
    causes: Set[str] = set()
    for node in downs:
        if node in root_set or any(nbr in up_reachable for nbr in neighbors(graph, node)):
            causes.add(node)
    return causes
