"""Ф9e: NetFlow v9/IPFIX collector adapter (identity from observed flows).

NetFlow is push, not poll: routers export flow records to a UDP collector that must
listen continuously and cache templates across packets. So this adapter runs a
bounded, fail-soft background receiver; ``collect()`` drains the accumulated identity
observations. A flow record yields an identity hint ONLY when the exporter includes a
MAC field (``IN_SRC_MAC``/``OUT_DST_MAC``) -- without a MAC there is nothing to merge
on (UNKNOWN over a guess). Every observed IP is RFC1918-gated; a public peer never
enters ``net_*``. The parser (third-party ``netflow``) is injected here so the suite
never binds a socket or builds a real packet. Traffic-edge overlay is a documented
carry-forward; this increment delivers identity only. RED first.
"""

from __future__ import annotations

import struct
from typing import Any, List

import pytest
from server.netdisco.adapters.base import AdapterConfig
from server.netdisco.adapters.flow import (
    FlowReceiver,
    _identities_from_flow,
    _to_ip,
    _to_mac,
)

_SRC_MAC_BYTES = b"\xaa\xbb\xcc\x00\x00\x01"
_DST_MAC_BYTES = b"\xaa\xbb\xcc\x00\x00\x02"


def _cfg() -> AdapterConfig:
    return AdapterConfig(adapter_type="flow", endpoint="10.0.0.2")


# --- value normalisation -----------------------------------------------------


def test_to_ip_accepts_rfc1918_string() -> None:
    assert _to_ip("10.0.0.5") == "10.0.0.5"
    assert _to_ip("192.168.1.9") == "192.168.1.9"


def test_to_ip_drops_public_and_garbage() -> None:
    assert _to_ip("8.8.8.8") is None
    assert _to_ip("not-an-ip") is None
    assert _to_ip(None) is None


def test_to_ip_accepts_int_and_bytes() -> None:
    assert _to_ip(0x0A000005) == "10.0.0.5"  # 10.0.0.5 as int
    assert _to_ip(b"\x0a\x00\x00\x06") == "10.0.0.6"  # 10.0.0.6 as 4 bytes


def test_to_mac_from_six_bytes() -> None:
    assert _to_mac(_SRC_MAC_BYTES) == "aa:bb:cc:00:00:01"


def test_to_mac_rejects_bad_shapes() -> None:
    assert _to_mac(b"\x00\x00") is None  # not 6 bytes
    assert _to_mac("") is None
    assert _to_mac(None) is None


def test_to_mac_drops_broadcast_multicast_and_zero() -> None:
    # A garbage/hostile exporter must not seed junk nodes from non-host MACs.
    assert _to_mac(b"\xff\xff\xff\xff\xff\xff") is None  # broadcast
    assert _to_mac(b"\x01\x00\x5e\x00\x00\x01") is None  # IPv4 multicast
    assert _to_mac(b"\x00\x00\x00\x00\x00\x00") is None  # all-zero
    assert _to_mac(b"\xaa\xbb\xcc\x00\x00\x01") == "aa:bb:cc:00:00:01"  # real unicast kept


# --- flow record -> identity hints -------------------------------------------


def test_flow_yields_src_and_dst_when_macs_present() -> None:
    data = {
        "IPV4_SRC_ADDR": "10.0.0.5",
        "IN_SRC_MAC": _SRC_MAC_BYTES,
        "IPV4_DST_ADDR": "10.0.0.6",
        "OUT_DST_MAC": _DST_MAC_BYTES,
    }
    nodes = _identities_from_flow(data)
    by_ip = {n.ip: n for n in nodes}
    assert set(by_ip) == {"10.0.0.5", "10.0.0.6"}
    assert all(n.dev_type == "endpoint" for n in nodes)
    assert by_ip["10.0.0.5"].mac == "aa:bb:cc:00:00:01"


def test_flow_without_mac_yields_nothing() -> None:
    # IP-only flow (no MAC field) -> nothing to merge on -> no hint (UNKNOWN over guess)
    data = {"IPV4_SRC_ADDR": "10.0.0.5", "IPV4_DST_ADDR": "10.0.0.6"}
    assert _identities_from_flow(data) == []


def test_flow_drops_public_ip_keeps_mac() -> None:
    data = {"IPV4_SRC_ADDR": "8.8.8.8", "IN_SRC_MAC": _SRC_MAC_BYTES}
    nodes = _identities_from_flow(data)
    assert len(nodes) == 1
    assert nodes[0].ip is None  # public IP dropped...
    assert nodes[0].mac == "aa:bb:cc:00:00:01"  # ...MAC still identifies


def test_flow_supports_ipfix_field_names() -> None:
    data = {"sourceIPv4Address": "10.0.0.7", "sourceMacAddress": _SRC_MAC_BYTES}
    nodes = _identities_from_flow(data)
    assert len(nodes) == 1 and nodes[0].ip == "10.0.0.7"


# --- receiver (injected parse; no socket) ------------------------------------


class _Flow:
    def __init__(self, data: dict) -> None:
        self.data = data


class _Packet:
    def __init__(self, flows: List[_Flow]) -> None:
        self.flows = flows


def _good_parse(data: bytes, templates: Any) -> _Packet:
    return _Packet([_Flow({"IPV4_SRC_ADDR": "10.0.0.5", "IN_SRC_MAC": _SRC_MAC_BYTES})])


def test_receiver_ingest_then_drain() -> None:
    r = FlowReceiver(parse=_good_parse)
    r.ingest(b"whatever")
    nodes = r.drain()
    assert len(nodes) == 1 and nodes[0].mac == "aa:bb:cc:00:00:01"
    assert r.drain() == []  # drain clears the buffer


def test_receiver_ingest_isolates_parse_failure() -> None:
    def boom(data: bytes, templates: Any) -> Any:
        raise ValueError("template not recognized")

    r = FlowReceiver(parse=boom)
    r.ingest(b"x")  # must not raise
    assert r.drain() == []


def test_receiver_buffer_is_bounded() -> None:
    r = FlowReceiver(parse=_good_parse, max_buffer=10)
    for _ in range(50):
        r.ingest(b"x")
    assert len(r.drain()) <= 10  # ring buffer caps memory under a flood


def test_receiver_ignores_non_dict_flow_data() -> None:
    def parse(data: bytes, templates: Any) -> _Packet:
        return _Packet([_Flow("not-a-dict")])  # type: ignore[arg-type]

    r = FlowReceiver(parse=parse)
    r.ingest(b"x")
    assert r.drain() == []


# --- adapter -----------------------------------------------------------------


class _FakeReceiver:
    def __init__(self, nodes: list) -> None:
        self._nodes = nodes

    def drain(self) -> list:
        return self._nodes


def test_collect_drains_receiver() -> None:
    from server.netdisco.adapters.base import AdapterNode
    from server.netdisco.adapters.flow import FlowAdapter

    nodes = [AdapterNode(mac="aa:bb:cc:00:00:01", ip="10.0.0.5", dev_type="endpoint")]
    out = FlowAdapter(_cfg(), receiver=_FakeReceiver(nodes)).collect()
    assert len(out.nodes) == 1 and out.nodes[0].ip == "10.0.0.5"
    assert out.errors == ()


def test_collect_no_receiver_returns_error(monkeypatch: Any) -> None:
    from server.netdisco.adapters import flow

    monkeypatch.setattr(flow, "_ensure_receiver", lambda cfg: None)
    out = flow.FlowAdapter(_cfg()).collect()
    assert out.nodes == ()
    assert out.errors  # collector unavailable (no netflow dep / bind failed) -> error, no crash


def test_collect_never_raises(monkeypatch: Any) -> None:
    from server.netdisco.adapters import flow

    def boom(cfg: Any) -> Any:
        raise RuntimeError("bind exploded")

    monkeypatch.setattr(flow, "_ensure_receiver", boom)
    out = flow.FlowAdapter(_cfg()).collect()
    assert out.nodes == () and out.errors  # absorbed, never raises


def test_registered_in_scheduler_builders() -> None:
    from server.netdisco import scheduler
    from server.netdisco.adapters.flow import FlowAdapter

    assert scheduler._ADAPTER_BUILDERS.get("flow") is FlowAdapter


def test_flow_node_merges_by_mac() -> None:
    # A flow identity hint folds into net_* by MAC via the existing node-merge.
    from server.netdisco.adapter_merge import merge_adapter_result
    from server.netdisco.adapters.base import AdapterNode
    from server.netdisco.adapters.flow import FlowAdapter

    nodes = [AdapterNode(mac="aa:bb:cc:00:00:01", ip="10.0.0.5", dev_type="endpoint")]
    out = FlowAdapter(_cfg(), receiver=_FakeReceiver(nodes)).collect()
    added: List[dict] = []
    stats = merge_adapter_result(
        out, [], fill=lambda *a, **k: None, upsert=lambda row, now=None: added.append(row)
    )
    assert stats == {"enriched": 0, "added": 1}
    assert added[0]["mac"] == "aa:bb:cc:00:00:01"


def _v9_packet() -> bytes:
    """A real NetFlow v9 export: one template (256: src/dst IPv4 + src/dst MAC) + one
    data record (10.0.0.5 aa:bb:cc:00:00:01 -> 10.0.0.6 aa:bb:cc:00:00:02)."""
    fields = [(8, 4), (12, 4), (56, 6), (57, 6)]  # IPV4_SRC, IPV4_DST, IN_SRC_MAC, OUT_DST_MAC
    tmpl = struct.pack("!HH", 256, len(fields))
    for ftype, flen in fields:
        tmpl += struct.pack("!HH", ftype, flen)
    tmpl_fs = struct.pack("!HH", 0, 4 + len(tmpl)) + tmpl
    rec = (
        bytes([10, 0, 0, 5])
        + bytes([10, 0, 0, 6])
        + bytes.fromhex("aabbcc000001")
        + bytes.fromhex("aabbcc000002")
    )
    data_fs = struct.pack("!HH", 256, 4 + len(rec)) + rec
    header = struct.pack("!HHIIII", 9, 2, 1000, 1700000000, 1, 42)
    return header + tmpl_fs + data_fs


def test_real_netflow_v9_parse_end_to_end() -> None:
    # Validate the field-name/value format assumptions against the REAL netflow lib
    # (the injected-parse tests can't). Locks: dict template cache, IPs as strings,
    # MACs decoded as 48-bit ints. Skips cleanly where the optional dep is absent.
    pytest.importorskip("netflow")
    r = FlowReceiver()  # parse=None -> lazy real netflow.parse_packet
    r.ingest(_v9_packet())
    nodes = {n.ip: n for n in r.drain()}
    assert nodes["10.0.0.5"].mac == "aa:bb:cc:00:00:01"
    assert nodes["10.0.0.6"].mac == "aa:bb:cc:00:00:02"
    assert all(n.dev_type == "endpoint" for n in nodes.values())
