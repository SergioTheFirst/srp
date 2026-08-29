"""Ф9a: MikroTik RouterOS REST adapter (identity: ARP + DHCP leases).

Read-only, fail-soft: ``collect()`` must never raise. Identity is merged per MAC
(IP from ARP, hostname from the DHCP lease); a public/garbage IP is dropped
(RFC1918 only), a hostname is sanitised at the boundary, and a failure on one
endpoint is isolated in ``errors`` without losing the other. The HTTP transport
is injected so the suite never opens a socket. RED first.
"""

from __future__ import annotations

from typing import Any, Dict, List

from server.netdisco.adapters.base import AdapterConfig
from server.netdisco.adapters.mikrotik import MikroTikAdapter


def _adapter(transport) -> MikroTikAdapter:
    cfg = AdapterConfig(adapter_type="mikrotik", endpoint="10.0.0.1", credential="mt")
    return MikroTikAdapter(cfg, transport=transport)


def _by_mac(result) -> Dict[str, Any]:
    from server.analytics.oui import normalize_mac

    return {normalize_mac(n.mac): n for n in result.nodes}


def test_collect_merges_arp_and_dhcp_by_mac() -> None:
    def transport(path: str) -> List[dict]:
        if path.endswith("/ip/arp"):
            return [{"address": "10.0.0.50", "mac-address": "AA:BB:CC:00:00:01"}]
        if path.endswith("/lease"):
            return [
                {
                    "address": "10.0.0.50",
                    "mac-address": "AA:BB:CC:00:00:01",
                    "host-name": "alices-pc",
                }
            ]
        return []

    out = _adapter(transport).collect()
    assert out.errors == ()
    node = _by_mac(out)["aabbcc000001"] if "aabbcc000001" in _by_mac(out) else list(out.nodes)[0]
    assert node.ip == "10.0.0.50"
    assert node.hostname == "alices-pc"


def test_collect_drops_public_ip() -> None:
    def transport(path: str) -> List[dict]:
        if path.endswith("/ip/arp"):
            return [{"address": "8.8.8.8", "mac-address": "AA:BB:CC:00:00:02"}]
        return []

    out = _adapter(transport).collect()
    # the MAC may still appear (seen on the router) but its public IP must not be imported
    assert all(n.ip != "8.8.8.8" for n in out.nodes)


def test_collect_sanitises_hostname() -> None:
    def transport(path: str) -> List[dict]:
        if path.endswith("/lease"):
            return [
                {
                    "address": "10.0.0.7",
                    "mac-address": "AA:BB:CC:00:00:03",
                    "host-name": "bad\x00name",
                }
            ]
        return []

    out = _adapter(transport).collect()
    assert all(n.hostname != "bad\x00name" for n in out.nodes)


def test_collect_isolates_endpoint_error() -> None:
    def transport(path: str) -> List[dict]:
        if path.endswith("/ip/arp"):
            raise OSError("connection refused")
        if path.endswith("/lease"):
            return [{"address": "10.0.0.8", "mac-address": "AA:BB:CC:00:00:04", "host-name": "pc8"}]
        return []

    out = _adapter(transport).collect()
    assert out.errors  # the ARP failure is recorded
    assert any(n.hostname == "pc8" for n in out.nodes)  # ...but the lease still parsed


def test_collect_never_raises_on_garbage() -> None:
    out = _adapter(lambda path: "not-a-list").collect()
    assert out.nodes == ()  # garbage -> nothing, no exception


def test_collect_no_credential_returns_error_not_crash() -> None:
    # No injected transport and no store -> cannot authenticate; empty + error.
    cfg = AdapterConfig(adapter_type="mikrotik", endpoint="10.0.0.1", credential="mt")
    out = MikroTikAdapter(cfg).collect()
    assert out.nodes == ()
    assert out.errors
