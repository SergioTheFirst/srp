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


# --- S4 chokepoints: articulation points & bridges (single-point-of-failure overlay) ---
# triangle a-b-c-a with a tail c-d -> proves cycle members are NOT cut vertices/bridges.
_CYCLE_LINKS = [
    {"a_nid": "a", "b_nid": "b"},
    {"a_nid": "b", "b_nid": "c"},
    {"a_nid": "c", "b_nid": "a"},
    {"a_nid": "c", "b_nid": "d"},
]
_CYCLE_DEVICES = [{"device_nid": n} for n in ("a", "b", "c", "d")]


def test_find_articulation_points_in_a_tree_are_the_internal_nodes():
    # gw and sw split the tree if removed; leaves + isolated 'lonely' never do.
    assert g.find_articulation_points(_graph()) == {"gw", "sw"}


def test_find_articulation_points_excludes_nodes_inside_a_cycle():
    graph = g.build_graph(_CYCLE_DEVICES, _CYCLE_LINKS)
    # only 'c' (joins the redundant triangle to the dead-end tail 'd') is a SPOF
    assert g.find_articulation_points(graph) == {"c"}


def test_find_bridges_in_a_tree_is_every_edge():
    assert g.find_bridges(_graph()) == {
        frozenset({"R", "gw"}),
        frozenset({"gw", "h1"}),
        frozenset({"gw", "h2"}),
        frozenset({"gw", "sw"}),
        frozenset({"sw", "h3"}),
    }


def test_find_bridges_excludes_edges_inside_a_cycle():
    graph = g.build_graph(_CYCLE_DEVICES, _CYCLE_LINKS)
    # the three triangle edges are redundant; only the tail c-d is a bridge
    assert g.find_bridges(graph) == {frozenset({"c", "d"})}
