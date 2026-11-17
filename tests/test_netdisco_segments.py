"""Segment links and life evidence from agent snapshots (pure, no DB)."""

from __future__ import annotations

import pytest
from server.netdisco.segments import alive_nids, segment_links

pytestmark = pytest.mark.unit

GW_MAC, PC_MAC = "AA-00-00-00-00-01", "AA-00-00-00-00-02"
NOW = "2026-11-11T12:00:00+00:00"
OLD = "2026-11-10T12:00:00+00:00"
KNOWN = {GW_MAC: "gw", PC_MAC: "pc"}


def resolve(mac, ip):
    return KNOWN.get(mac)


def snap(did="d1", at=NOW, up=True, role="lan", tunnel=False, nb_ip="10.0.0.20", state="Reachable"):
    return {
        "device_id": did,
        "last_seen": at,
        "snapshot_at": at,
        "adapters": [
            {
                "mac": "AA-00-00-00-00-09",
                "up": up,
                "role": role,
                "tunnel": tunnel,
                "ipv4": ["10.0.0.5"],
                "gateway": "10.0.0.1",
            }
        ],
        "neighbors": [
            {"ip": "10.0.0.1", "mac": GW_MAC, "state": "Reachable"},
            {"ip": nb_ip, "mac": PC_MAC, "state": state},
        ],
        "quality": [{"target_kind": "gateway", "target": "10.0.0.1", "loss_pct": 0.0}],
    }


FUTURE = "2026-11-11T22:00:00+00:00"  # agent clock 10 h ahead
EIGHT_H_AGO = "2026-11-11T04:00:00+00:00"


def alive(s):
    return set(alive_nids([s], resolve, now=NOW, fresh_sec=3600, agent_fresh_sec=3600))


def test_one_observer_one_link():
    (lk,) = segment_links([snap()], resolve, set())
    assert (lk["a"], lk["b"], lk["observers"]) == ("pc", "gw", 1)
    assert lk["link_kind"] == "l2-segment" and lk["medium"] == "unknown"


def test_two_observers():
    (lk,) = segment_links([snap("d1"), snap("d2")], resolve, set())
    assert lk["observers"] == 2


@pytest.mark.parametrize(
    "kw", [{"nb_ip": "10.0.1.20"}, {"up": False}, {"tunnel": True}, {"role": "virtual"}]
)
def test_skipped(kw):
    assert segment_links([snap(**kw)], resolve, set()) == []


def test_gateway_self_pair_skipped():
    s = snap(nb_ip="10.0.0.1")
    s["neighbors"][1]["mac"] = GW_MAC
    assert segment_links([s], resolve, set()) == []


def test_skip_nids():
    assert segment_links([snap()], resolve, {"pc"}) == []


def test_stale_snapshot_ignored():
    (lk,) = segment_links([snap("d1"), snap("d2", at=OLD)], resolve, set())
    assert lk["observers"] == 1


def test_unresolved():
    assert segment_links([snap()], lambda m, i: None if m == PC_MAC else "gw", set()) == []
    assert segment_links([snap()], lambda m, i: None if m == GW_MAC else "pc", set()) == []


def test_alive_agent_only():
    s = snap()
    s["adapters"][0]["mac"] = PC_MAC
    s["neighbors"] = []
    s["quality"] = []
    assert alive(s) == {"pc"}


def test_alive_reachable_neighbour_only():
    s = snap()
    s["quality"] = []
    s["neighbors"][0]["state"] = "Stale"
    assert alive(s) == {"pc"}


def test_alive_gateway_by_quality():
    s = snap()
    s["neighbors"][1]["state"] = "Stale"
    s["neighbors"][0]["state"] = "Stale"
    assert alive(s) == {"gw"}
    s["quality"][0]["loss_pct"] = 100.0
    assert alive(s) == set()


def test_alive_stale_excluded():
    assert alive(snap(at=OLD)) == set()


def test_alive_returns_newest_evidence_time():
    older = snap("d1", at="2026-11-11T11:50:00+00:00")
    out = alive_nids([older, snap("d2")], resolve, now=NOW, fresh_sec=3600, agent_fresh_sec=0)
    assert out == {"pc": NOW, "gw": NOW}


def _no_quality(**kw):
    s = snap(**kw)
    s["quality"] = []
    s["neighbors"][0]["state"] = "Stale"
    return s


def test_alive_neighbour_outside_adapter_subnet():
    assert alive(_no_quality(nb_ip="10.0.1.20")) == set()


def test_alive_neighbour_not_rfc1918():
    s = _no_quality(nb_ip="8.8.8.20")
    s["adapters"][0].update(ipv4=["8.8.8.5"], gateway="8.8.8.1")
    assert alive(s) == set()


@pytest.mark.parametrize("kw", [{"tunnel": True}, {"up": False}])
def test_alive_only_through_tunnel_or_down_adapter(kw):
    assert alive(snap(**kw)) == set()


def test_alive_quality_target_not_a_gateway():
    s = _no_quality()
    s["neighbors"][1]["state"] = "Stale"
    s["neighbors"].append({"ip": "10.0.0.99", "mac": GW_MAC, "state": "Stale"})
    s["quality"] = [{"target_kind": "gateway", "target": "10.0.0.99", "loss_pct": 0.0}]
    assert alive(s) == set()


def _future_clock():
    s = snap("d3")
    s.update(snapshot_at=FUTURE, last_seen=EIGHT_H_AGO)
    return s


def test_future_agent_clock_is_stale_and_harmless_for_links():
    (lk,) = segment_links([snap("d1"), snap("d2"), _future_clock()], resolve, set())
    assert lk["observers"] == 2


def test_future_agent_clock_gives_no_liveness():
    assert alive(_future_clock()) == set()
    s = snap()
    s.update(snapshot_at=FUTURE, last_seen=None)
    assert alive(s) == set()


def test_silent_observer_vouches_for_nobody():
    s = snap()
    s["last_seen"] = "2026-11-11T09:00:00+00:00"  # agent silent 3 h, snapshot time itself fresh
    s["snapshot_at"] = "2026-11-11T11:30:00+00:00"
    out = alive_nids([s], resolve, now=NOW, fresh_sec=36000, agent_fresh_sec=3600)
    assert out == {}
    s["last_seen"] = "2026-11-11T11:45:00+00:00"
    out = alive_nids([s], resolve, now=NOW, fresh_sec=36000, agent_fresh_sec=3600)
    assert set(out) == {"pc", "gw"}


def test_adapter_vouches_for_first_two_addresses_only():
    s = snap(nb_ip="10.0.2.20")
    s["adapters"][0]["ipv4"] = ["10.0.0.5", "10.0.1.5", "10.0.2.5"]
    assert alive(s) == {"gw"}  # third /24 ignored; gateway still vouched by quality
    s["neighbors"][1]["ip"] = "10.0.1.20"
    assert alive(s) == {"pc", "gw"}


def test_fifth_usable_adapter_ignored():
    s = snap(nb_ip="10.0.9.20")
    base = s["adapters"][0]
    s["adapters"] = [dict(base, ipv4=[f"10.0.{i}.5"], gateway=f"10.0.{i}.1") for i in range(5)]
    s["adapters"][4]["ipv4"] = ["10.0.9.5"]
    s["quality"] = []
    s["neighbors"][0]["state"] = "Stale"
    assert alive(s) == set()
    assert segment_links([s], resolve, set()) == []


def test_agent_with_public_primary_ip_resolves_by_mac_only():
    seen = []

    def res(mac, ip):
        seen.append(ip)
        return None

    s = snap()
    s["adapters"][0]["ipv4"] = ["8.8.8.5"]
    alive_nids([s], res, now=NOW, fresh_sec=3600, agent_fresh_sec=3600)
    assert seen[0] is None
