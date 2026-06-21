"""Phase 10 -- §1.5/§3.7 reachability correlation (RED first).

Turns a raw ``down_set`` into DOWN vs UNREACHABLE: a node whose path to the root
crosses another down node is UNREACHABLE (its alarms suppressed, one root-cause
raised on the upstream node), not independently DOWN. Kills the alarm storm: one
gateway failure shows one cause, not a hundred symptoms. Pure function.
"""

from __future__ import annotations

from server.netdisco import correlation
from server.netdisco import graph as g

_LINKS = [
    {"a_nid": "R", "b_nid": "gw"},
    {"a_nid": "gw", "b_nid": "h1"},
    {"a_nid": "gw", "b_nid": "h2"},
    {"a_nid": "gw", "b_nid": "sw"},
    {"a_nid": "sw", "b_nid": "h3"},
]
_DEVICES = [{"device_nid": n} for n in ("R", "gw", "sw", "h1", "h2", "h3")]


def _graph():
    return g.build_graph(_DEVICES, _LINKS)


def test_gateway_failure_yields_one_cause_and_suppressed_downstream():
    verdicts = correlation.correlate(_graph(), down_set={"gw", "h1", "h2", "h3"}, roots={"R"})
    assert verdicts["gw"] == correlation.Verdict(
        status=correlation.DOWN, root_cause="gw", suppressed=False
    )
    for stranded in ("h1", "h2", "h3"):
        assert verdicts[stranded].status == correlation.UNREACHABLE
        assert verdicts[stranded].root_cause == "gw"  # one cause, the gateway
        assert verdicts[stranded].suppressed is True


def test_independent_edge_failures_are_both_down_not_suppressed():
    verdicts = correlation.correlate(_graph(), down_set={"h1", "h2"}, roots={"R"})
    assert {k: v.status for k, v in verdicts.items()} == {
        "h1": correlation.DOWN,
        "h2": correlation.DOWN,
    }
    assert all(v.suppressed is False for v in verdicts.values())


def test_deep_chain_attributes_topmost_cause():
    links = [
        {"a_nid": "R", "b_nid": "A"},
        {"a_nid": "A", "b_nid": "B"},
        {"a_nid": "B", "b_nid": "C"},
    ]
    graph = g.build_graph([{"device_nid": n} for n in ("R", "A", "B", "C")], links)
    verdicts = correlation.correlate(graph, down_set={"A", "B", "C"}, roots={"R"})
    assert verdicts["A"].status == correlation.DOWN
    assert verdicts["B"].root_cause == "A" and verdicts["B"].suppressed
    assert verdicts["C"].root_cause == "A" and verdicts["C"].suppressed


def test_empty_down_set_is_empty():
    assert correlation.correlate(_graph(), down_set=set(), roots={"R"}) == {}
