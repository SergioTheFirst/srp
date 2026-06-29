"""Ф9d: adapter link-merge — persist AdapterResult.links into net_links additively.

The adapters (UniFi/MikroTik/Redfish) already collect LLDP/uplink/neighbour links;
until now those were carried but dropped. This increment persists them, with three
hard rules:

1. **Never downgrade a validated SNMP edge.** Adapter links go in under their own
   ``link_kind="adapter"`` via an ADDITIVE writer (``db.add_adapter_link`` =
   INSERT ON CONFLICT DO NOTHING). The latest-wins ``db.upsert_net_link`` must never
   be used for adapter data.
2. **Never fabricate an edge from junk.** Each endpoint MAC goes through
   ``normalize_mac`` (drop on None) so a malformed ``chassis_id`` can't seed a fake
   link; a known MAC resolves to its canonical nid, an unknown well-formed MAC to a
   ``nd-mac-`` stub.
3. **Never draw a parallel edge.** ``unified._real_links`` gap-fills: an adapter edge
   is drawn only when no validated link already connects that node pair.

RED first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List

import server.db as db
from server.netdisco.adapter_merge import merge_adapter_links
from server.netdisco.adapters.base import AdapterLink, AdapterResult
from server.netdisco.identity import device_nid
from server.netdisco.unified import _real_links

_MAC_A = "aa:bb:cc:00:00:01"
_MAC_B = "aa:bb:cc:00:00:02"
_NID_A = "nd-agent-a"  # a known device's canonical nid (not the bare nd-mac form)


# --- db.add_adapter_link (additive writer) -----------------------------------


def _link(a: str, b: str, **kw: Any) -> dict:
    base = {
        "a_nid": a,
        "b_nid": b,
        "link_kind": "adapter",
        "via_source": "adapter:unifi",
        "confidence": "low",
    }
    base.update(kw)
    return base


def test_add_adapter_link_persists_a_new_edge(tmp_path: Path) -> None:
    db.init_db(tmp_path / "srp.db")
    db.add_adapter_link(_link("nd-x", "nd-y"))
    rows = db.get_net_links()
    assert len(rows) == 1
    assert {rows[0]["a_nid"], rows[0]["b_nid"]} == {"nd-x", "nd-y"}
    assert rows[0]["link_kind"] == "adapter"
    assert rows[0]["via_source"] == "adapter:unifi"


def test_add_adapter_link_is_idempotent(tmp_path: Path) -> None:
    db.init_db(tmp_path / "srp.db")
    db.add_adapter_link(_link("nd-x", "nd-y"))
    db.add_adapter_link(_link("nd-x", "nd-y", confidence="high"))  # rerun, different conf
    rows = db.get_net_links()
    assert len(rows) == 1  # ON CONFLICT DO NOTHING -> no duplicate...
    assert rows[0]["confidence"] == "low"  # ...and the first row is NOT modified


def test_add_adapter_link_never_downgrades_snmp_edge(tmp_path: Path) -> None:
    db.init_db(tmp_path / "srp.db")
    # A validated SNMP LLDP edge between the pair.
    db.upsert_net_link(
        {
            "a_nid": "nd-x",
            "b_nid": "nd-y",
            "link_kind": "l2-edge",
            "via_source": "lldp",
            "confidence": "high",
        }
    )
    db.add_adapter_link(_link("nd-x", "nd-y"))  # adapter edge, different link_kind
    rows = {r["link_kind"]: r for r in db.get_net_links()}
    assert rows["l2-edge"]["via_source"] == "lldp"  # SNMP row untouched
    assert rows["l2-edge"]["confidence"] == "high"
    assert "adapter" in rows  # adapter row stored separately


def test_add_adapter_link_canonicalises_endpoints(tmp_path: Path) -> None:
    db.init_db(tmp_path / "srp.db")
    db.add_adapter_link(_link("nd-zzz", "nd-aaa"))  # a_nid > b_nid
    row = db.get_net_links()[0]
    assert row["a_nid"] <= row["b_nid"]  # stored canonical


# --- adapter_merge.merge_adapter_links (resolve + dedup) ----------------------


def _collect_links() -> tuple[List[dict], Any]:
    calls: List[dict] = []

    def add_link(link: dict, now: Any = None) -> None:
        calls.append(link)

    return calls, add_link


def test_merge_links_resolves_known_and_stub_macs() -> None:
    calls, add_link = _collect_links()
    known = [{"device_nid": _NID_A, "mac": _MAC_A}]
    result = AdapterResult(links=(AdapterLink(a_mac=_MAC_A, b_mac=_MAC_B, link_kind="lldp"),))
    n = merge_adapter_links(result, known, adapter_type="unifi", add_link=add_link)
    assert n == 1
    link = calls[0]
    assert {link["a_nid"], link["b_nid"]} == {_NID_A, device_nid(mac=_MAC_B)}
    assert link["via_source"].startswith("adapter")
    assert link["confidence"] == "low"
    assert link["link_kind"] == "adapter"


def test_merge_links_drops_malformed_mac() -> None:
    calls, add_link = _collect_links()
    result = AdapterResult(links=(AdapterLink(a_mac=_MAC_A, b_mac="not-a-mac"),))
    n = merge_adapter_links(result, [], add_link=add_link)
    assert n == 0
    assert calls == []  # a junk chassis_id can never seed a fake edge


def test_merge_links_drops_self_loop() -> None:
    calls, add_link = _collect_links()
    result = AdapterResult(links=(AdapterLink(a_mac=_MAC_A, b_mac=_MAC_A),))
    n = merge_adapter_links(result, [], add_link=add_link)
    assert n == 0
    assert calls == []


def test_merge_links_dedups_pair_within_batch() -> None:
    calls, add_link = _collect_links()
    result = AdapterResult(
        links=(
            AdapterLink(a_mac=_MAC_A, b_mac=_MAC_B, link_kind="uplink"),
            AdapterLink(a_mac=_MAC_B, b_mac=_MAC_A, link_kind="lldp"),  # same pair, reversed
        )
    )
    n = merge_adapter_links(result, [], add_link=add_link)
    assert n == 1  # one canonical edge, not two
    assert len(calls) == 1


def test_merge_links_preserves_port_labels() -> None:
    calls, add_link = _collect_links()
    result = AdapterResult(
        links=(AdapterLink(a_mac=_MAC_A, b_mac=_MAC_B, a_port="1", b_port="Port 5"),)
    )
    merge_adapter_links(result, [], add_link=add_link)
    assert calls[0]["a_port"] in ("1", "Port 5")
    assert calls[0]["b_port"] in ("1", "Port 5")


# --- unified._real_links gap-fill --------------------------------------------


def test_real_links_hides_adapter_edge_parallel_to_validated_link() -> None:
    net_links = [
        {
            "a_nid": "X",
            "b_nid": "Y",
            "link_kind": "l2-edge",
            "via_source": "lldp",
            "confidence": "high",
        },
        {
            "a_nid": "X",
            "b_nid": "Y",
            "link_kind": "adapter",
            "via_source": "adapter:unifi",
            "confidence": "low",
        },
    ]
    out = _real_links(net_links, {}, {})
    pairs = [(e["a"], e["b"]) for e in out]
    assert pairs.count(("X", "Y")) == 1  # only ONE X-Y edge
    assert out[0]["via_source"] == "lldp"  # ...and it is the validated one


def test_real_links_draws_adapter_edge_when_no_validated_link() -> None:
    net_links = [
        {
            "a_nid": "X",
            "b_nid": "Z",
            "link_kind": "adapter",
            "via_source": "adapter:unifi",
            "confidence": "low",
        },
    ]
    out = _real_links(net_links, {}, {})
    assert any(e["a"] == "X" and e["b"] == "Z" for e in out)  # gap filled


def test_real_links_keeps_all_validated_links() -> None:
    # No adapter links -> behaviour unchanged: every validated edge is drawn.
    net_links = [
        {"a_nid": "X", "b_nid": "Y", "link_kind": "l2-edge", "via_source": "lldp"},
        {"a_nid": "Y", "b_nid": "Z", "link_kind": "l2-edge", "via_source": "cdp"},
    ]
    out = _real_links(net_links, {}, {})
    assert len(out) == 2


# --- scheduler.run_adapter_cycle wiring --------------------------------------


def test_run_adapter_cycle_merges_links() -> None:
    from dataclasses import dataclass, field

    from server.netdisco.adapters.base import AdapterConfig

    @dataclass
    class _Cfg:
        enabled: bool = True
        optional_adapters: tuple = field(
            default_factory=lambda: (
                AdapterConfig(adapter_type="unifi", endpoint="10.0.0.1", credential="u"),
            )
        )

    result = AdapterResult(
        nodes=(),
        links=(AdapterLink(a_mac=_MAC_A, b_mac=_MAC_B, link_kind="lldp"),),
    )

    class _Builder:
        def __init__(self, cfg: Any, store: Any = None) -> None:
            pass

        def collect(self) -> AdapterResult:
            return result

    seen: List[dict] = []

    def fake_link_merge(
        res: AdapterResult, known: Any, *, adapter_type: str = "", **kw: Any
    ) -> int:
        seen.append({"adapter_type": adapter_type, "links": len(res.links)})
        return len(res.links)

    from server.netdisco import scheduler

    out = scheduler.run_adapter_cycle(
        _Cfg(),
        get_known=lambda: [],
        merge=lambda res, known, now=None: {"enriched": 0, "added": 0},
        builders={"unifi": _Builder},
        link_merge=fake_link_merge,
        store=object(),
    )
    assert out["links"] == 1
    assert seen and seen[0]["adapter_type"] == "unifi"
