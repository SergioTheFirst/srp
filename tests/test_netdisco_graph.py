"""Phase 10 -- §3.5 graph engine (RED first).

A pure in-memory undirected graph built from net_devices + net_links. BFS gives
reachability from the roots (a blocked/down node stops traversal), the shortest path
to a root, and the root-cause set (the topmost down nodes -- those still adjacent to
the up-reachable component). All functions are pure: identical inputs -> identical
output, no DB, no clock.
"""

from __future__ import annotations

from server.netdisco import graph as g

# R --- gw --- h1
#         |  \-- h2
#         \-- sw --- h3
_LINKS = [
    {"a_nid": "R", "b_nid": "gw"},
    {"a_nid": "gw", "b_nid": "h1"},
    {"a_nid": "gw", "b_nid": "h2"},
    {"a_nid": "gw", "b_nid": "sw"},
    {"a_nid": "sw", "b_nid": "h3"},
]
_DEVICES = [{"device_nid": n} for n in ("R", "gw", "sw", "h1", "h2", "h3", "lonely")]


def _graph():
    return g.build_graph(_DEVICES, _LINKS)


def test_build_graph_is_undirected_and_includes_isolated_nodes():
    graph = _graph()
    assert g.neighbors(graph, "gw") == frozenset({"R", "h1", "h2", "sw"})
    assert g.neighbors(graph, "h3") == frozenset({"sw"})
    assert g.neighbors(graph, "lonely") == frozenset()  # isolated device still a node


def test_reachable_from_root_reaches_whole_connected_component():
    reached = g.reachable_from(_graph(), {"R"})
    assert reached == {"R", "gw", "sw", "h1", "h2", "h3"}  # 'lonely' is not connected


def test_blocked_node_stops_traversal():
    # gw down -> nothing behind it is reachable from R
    assert g.reachable_from(_graph(), {"R"}, blocked={"gw"}) == {"R"}


def test_path_to_root_is_shortest_hop_chain():
    assert g.path_to_root(_graph(), "h3", {"R"}) == ["h3", "sw", "gw", "R"]
    assert g.path_to_root(_graph(), "R", {"R"}) == ["R"]
    assert g.path_to_root(_graph(), "lonely", {"R"}) is None  # no path


def test_find_root_cause_is_the_topmost_down_node():
    # gw, h1 and h3 are down; only gw is directly reachable from the root, so it is
    # the single root cause -- h1/h3 are merely stranded behind it.
    rc = g.find_root_cause(_graph(), down_set={"gw", "h1", "h3"}, roots={"R"})
    assert rc == {"gw"}


def test_two_independent_failures_are_both_root_causes():
    # h1 and h2 both hang off gw (up) -> two independent edge failures, both causes
    rc = g.find_root_cause(_graph(), down_set={"h1", "h2"}, roots={"R"})
    assert rc == {"h1", "h2"}
