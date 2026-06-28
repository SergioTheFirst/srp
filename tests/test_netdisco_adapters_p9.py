"""Ф9a: optional active-equipment adapters — base contract + MAC-merge + cycle.

Adapters pull ready topology/identity from controllers the operator owns (their
credentials), read-only and isolated: ``collect()`` must NEVER raise (a failure is
reported in ``AdapterResult.errors``), and the merge is by normalised MAC and only
ever ENRICHES -- it never overrides a validated SNMP identity (it reuses the Ф8
fill-empty writer). RED first.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from server.netdisco.adapters.base import (
    AdapterConfig,
    AdapterNode,
    AdapterResult,
    NetworkAdapter,
)

# --------------------------------------------------------------------------- #
# base contract                                                                #
# --------------------------------------------------------------------------- #


def test_adapter_config_frozen_with_safe_defaults() -> None:
    cfg = AdapterConfig(adapter_type="mikrotik", endpoint="10.0.0.1")
    assert cfg.credential == "" and cfg.tls_verify is True and cfg.site_id == ""
    with pytest.raises(AttributeError):  # frozen dataclass -> FrozenInstanceError
        cfg.endpoint = "x"  # frozen


def test_adapter_result_defaults_empty() -> None:
    r = AdapterResult()
    assert r.nodes == () and r.links == () and r.errors == () and dict(r.identity_map) == {}


def test_network_adapter_is_abstract() -> None:
    with pytest.raises(TypeError):
        NetworkAdapter(AdapterConfig(adapter_type="x", endpoint="10.0.0.1"))  # type: ignore[abstract]


def test_concrete_adapter_collects() -> None:
    class _Fake(NetworkAdapter):
        def collect(self) -> AdapterResult:
            return AdapterResult(nodes=(AdapterNode(mac="aa:bb:cc:dd:ee:ff", ip="10.0.0.9"),))

    a = _Fake(AdapterConfig(adapter_type="fake", endpoint="10.0.0.1"))
    out = a.collect()
    assert a.config.endpoint == "10.0.0.1"
    assert out.nodes[0].ip == "10.0.0.9"


# --------------------------------------------------------------------------- #
# adapter_merge -- dedup by MAC, enrich-only, never override SNMP              #
# --------------------------------------------------------------------------- #


def _merge(result: AdapterResult, known: List[dict]):
    from server.netdisco import adapter_merge

    filled: Dict[str, dict] = {}
    added: List[dict] = []
    out = adapter_merge.merge_adapter_result(
        result,
        known,
        fill=lambda nid, **kw: filled.__setitem__(nid, {k: v for k, v in kw.items() if v}),
        upsert=lambda dev, now=None: added.append(dev),
    )
    return out, filled, added


def test_merge_enriches_existing_by_mac_fill_empty() -> None:
    known = [{"device_nid": "nd-1", "mac": "aa:bb:cc:dd:ee:01", "ip": "10.0.0.9"}]
    result = AdapterResult(
        nodes=(AdapterNode(mac="AA-BB-CC-DD-EE-01", hostname="ROUTER-OFFICE", subtype="router"),)
    )
    out, filled, added = _merge(result, known)
    assert out == {"enriched": 1, "added": 0}
    assert added == []  # existing node never re-created / overridden
    assert filled["nd-1"]["hostname"] == "ROUTER-OFFICE"  # MAC matched despite format diff
    assert filled["nd-1"]["subtype"] == "router"


def test_merge_adds_new_node_for_unknown_mac() -> None:
    result = AdapterResult(
        nodes=(AdapterNode(mac="aa:bb:cc:dd:ee:99", ip="10.0.0.50", hostname="NEW-PC"),)
    )
    out, filled, added = _merge(result, [])
    assert out == {"enriched": 0, "added": 1}
    assert filled == {}
    assert added[0]["ip"] == "10.0.0.50"
    assert added[0]["hostname"] == "NEW-PC"
    assert added[0]["status"] == "discovered"
    assert added[0]["dev_type"] == "endpoint"  # has a MAC


def test_merge_drops_public_ip_for_new_node() -> None:
    # A controller's ARP table may list a WAN/public peer; the merge must keep the
    # node (by MAC) but never import the public IP into net_* (M1 defense-in-depth).
    result = AdapterResult(
        nodes=(AdapterNode(mac="aa:bb:cc:dd:ee:77", ip="8.8.8.8", hostname="x"),)
    )
    out, filled, added = _merge(result, [])
    assert out["added"] == 1
    assert added[0]["ip"] is None


def test_merge_skips_macless_node() -> None:
    out, filled, added = _merge(AdapterResult(nodes=(AdapterNode(ip="10.0.0.7"),)), [])
    assert out == {"enriched": 0, "added": 0}  # no MAC -> cannot identify, skipped


def test_merge_dedups_within_one_result() -> None:
    nodes = (
        AdapterNode(mac="aa:bb:cc:dd:ee:02", ip="10.0.0.2"),
        AdapterNode(mac="AA:BB:CC:DD:EE:02", hostname="same-host"),  # same MAC, diff case
    )
    out, filled, added = _merge(AdapterResult(nodes=nodes), [])
    assert out["added"] == 1  # one node minted, second folds into it (enrich)
    assert out["enriched"] == 1


# --------------------------------------------------------------------------- #
# run_adapter_cycle -- gate, lock, per-adapter isolation                       #
# --------------------------------------------------------------------------- #


def _cfg(adapters=()):
    from server.netdisco.config import NetdiscoConfig

    return NetdiscoConfig(enabled=True, optional_adapters=tuple(adapters))


class _StubAdapter(NetworkAdapter):
    _result = AdapterResult(nodes=(AdapterNode(mac="aa:bb:cc:dd:ee:aa", ip="10.0.0.5"),))

    def __init__(self, config, *, store=None):
        super().__init__(config)

    def collect(self) -> AdapterResult:
        return self._result


def test_adapter_cycle_no_adapters_does_nothing() -> None:
    from server.netdisco import scheduler

    calls: List[str] = []
    out = scheduler.run_adapter_cycle(_cfg(), get_known=lambda: calls.append("k") or [])
    assert out == {"enriched": 0, "added": 0, "adapters": 0, "busy": 0}
    assert calls == []  # gated before any read


def test_adapter_cycle_runs_and_merges() -> None:
    from server.netdisco import scheduler

    merged: List[Any] = []
    out = scheduler.run_adapter_cycle(
        _cfg([AdapterConfig(adapter_type="mikrotik", endpoint="10.0.0.1")]),
        get_known=lambda: [],
        builders={"mikrotik": _StubAdapter},
        merge=lambda result, known, now=None: (
            merged.append(result) or {"enriched": 0, "added": len(result.nodes)}
        ),
        store=None,
    )
    assert out["adapters"] == 1
    assert out["added"] == 1
    assert merged and merged[0].nodes[0].ip == "10.0.0.5"


def test_adapter_cycle_skips_unknown_type() -> None:
    from server.netdisco import scheduler

    out = scheduler.run_adapter_cycle(
        _cfg([AdapterConfig(adapter_type="flow", endpoint="10.0.0.1")]),
        get_known=lambda: [],
        builders={"mikrotik": _StubAdapter},  # 'flow' not built here
        merge=lambda result, known, now=None: {"enriched": 0, "added": 0},
    )
    assert out["adapters"] == 0  # unimplemented type skipped, no crash


def test_adapter_cycle_isolates_one_failing_adapter() -> None:
    from server.netdisco import scheduler

    class _Boom(NetworkAdapter):
        def __init__(self, config, *, store=None):
            super().__init__(config)

        def collect(self) -> AdapterResult:
            raise RuntimeError("driver blew up")  # contract violation -> cycle must absorb

    out = scheduler.run_adapter_cycle(
        _cfg(
            [
                AdapterConfig(adapter_type="boom", endpoint="10.0.0.1"),
                AdapterConfig(adapter_type="mikrotik", endpoint="10.0.0.2"),
            ]
        ),
        get_known=lambda: [],
        builders={"boom": _Boom, "mikrotik": _StubAdapter},
        merge=lambda result, known, now=None: {"enriched": 0, "added": len(result.nodes)},
    )
    assert out["added"] == 1  # the good adapter still ran despite the bad one
