"""Ф9b: UniFi Controller adapter (identity: devices + clients; LLDP/uplink links).

Read-only, fail-soft: ``collect()`` must never raise. The controller answers with a
``{"meta": ..., "data": [...]}`` envelope; the adapter unwraps ``data``, folds device
and client rows into identity hints per MAC (device ``type`` -> dev_type, ``name`` ->
hostname, ``model``/``serial`` carried; client ``hostname``/``ip`` carried), and
emits LLDP/uplink links (carried for a later link-merge increment). A public/garbage
IP is dropped (RFC1918 only), a hostname is sanitised at the boundary, and a failure
on one endpoint is isolated in ``errors`` without losing the other. The HTTP transport
is injected so the suite never opens a socket. RED first.
"""

from __future__ import annotations

from typing import Any, Dict, List

from server.analytics.oui import normalize_mac
from server.netdisco.adapters.base import AdapterConfig
from server.netdisco.adapters.unifi import UniFiAdapter

_AP = "aa:bb:cc:00:00:10"
_GW = "aa:bb:cc:00:00:01"
_CLIENT = "aa:bb:cc:00:00:50"


def _adapter(transport, *, site_id: str = "") -> UniFiAdapter:
    cfg = AdapterConfig(
        adapter_type="unifi", endpoint="10.0.0.1", credential="uni", site_id=site_id
    )
    return UniFiAdapter(cfg, transport=transport)


def _envelope(rows: List[dict]) -> Dict[str, Any]:
    return {"meta": {"rc": "ok"}, "data": rows}


def _by_mac(result) -> Dict[str, Any]:
    return {normalize_mac(n.mac): n for n in result.nodes if n.mac}


def _device_client_transport(path: str) -> Dict[str, Any]:
    if path.endswith("/stat/device"):
        return _envelope(
            [
                {
                    "mac": _AP,
                    "ip": "10.0.0.10",
                    "name": "Office-AP",
                    "model": "U7PRO",
                    "type": "uap",
                    "serial": "ABC123",
                    "uplink": {"uplink_mac": _GW},
                    "lldp_table": [{"chassis_id": _GW, "port_id": "Port 5", "local_port_idx": 1}],
                }
            ]
        )
    if path.endswith("/stat/sta"):
        return _envelope(
            [{"mac": _CLIENT, "ip": "10.0.0.50", "hostname": "alices-pc", "name": "Alice"}]
        )
    return _envelope([])


def test_collect_parses_devices_and_clients() -> None:
    out = _adapter(_device_client_transport).collect()
    assert out.errors == ()
    by_mac = _by_mac(out)
    ap = by_mac[normalize_mac(_AP)]
    assert ap.dev_type == "ap"
    assert ap.subtype == "ap"
    assert ap.model == "U7PRO"
    assert ap.hostname == "Office-AP"
    assert ap.ip == "10.0.0.10"
    assert ap.serial == "ABC123"
    client = by_mac[normalize_mac(_CLIENT)]
    assert client.dev_type == "endpoint"
    assert client.hostname == "alices-pc"
    assert client.ip == "10.0.0.50"


def test_collect_builds_site_path_from_config() -> None:
    seen: List[str] = []

    def transport(path: str) -> Dict[str, Any]:
        seen.append(path)
        return _envelope([])

    _adapter(transport, site_id="hq").collect()
    assert any("/s/hq/stat/device" in p for p in seen)
    assert any("/s/hq/stat/sta" in p for p in seen)


def test_collect_defaults_site_to_default() -> None:
    seen: List[str] = []

    def transport(path: str) -> Dict[str, Any]:
        seen.append(path)
        return _envelope([])

    _adapter(transport, site_id="").collect()  # empty site -> "default"
    assert any("/s/default/stat/device" in p for p in seen)


def test_collect_maps_device_types() -> None:
    rows = [
        {"mac": "aa:bb:cc:00:01:01", "type": "usw", "name": "sw1"},
        {"mac": "aa:bb:cc:00:01:02", "type": "ugw", "name": "gw1"},
        {"mac": "aa:bb:cc:00:01:03", "type": "uph", "name": "phone1"},
    ]

    def transport(path: str) -> Dict[str, Any]:
        return _envelope(rows) if path.endswith("/stat/device") else _envelope([])

    by_mac = _by_mac(_adapter(transport).collect())
    assert by_mac[normalize_mac("aa:bb:cc:00:01:01")].dev_type == "switch"
    assert by_mac[normalize_mac("aa:bb:cc:00:01:02")].dev_type == "router"
    assert by_mac[normalize_mac("aa:bb:cc:00:01:03")].dev_type == "phone"


def test_collect_builds_lldp_and_uplink_links() -> None:
    out = _adapter(_device_client_transport).collect()
    # The AP reports an uplink and an LLDP neighbour, both pointing at the gateway.
    assert any(
        normalize_mac(link.a_mac) == normalize_mac(_AP)
        and normalize_mac(link.b_mac) == normalize_mac(_GW)
        for link in out.links
    )


def test_collect_drops_public_ip() -> None:
    def transport(path: str) -> Dict[str, Any]:
        if path.endswith("/stat/sta"):
            return _envelope([{"mac": "aa:bb:cc:00:02:01", "ip": "8.8.8.8", "hostname": "x"}])
        return _envelope([])

    out = _adapter(transport).collect()
    assert all(n.ip != "8.8.8.8" for n in out.nodes)


def test_collect_sanitises_hostname() -> None:
    def transport(path: str) -> Dict[str, Any]:
        if path.endswith("/stat/sta"):
            return _envelope(
                [{"mac": "aa:bb:cc:00:02:02", "ip": "10.0.0.7", "hostname": "bad\x00name"}]
            )
        return _envelope([])

    out = _adapter(transport).collect()
    assert all(n.hostname != "bad\x00name" for n in out.nodes)


def test_collect_isolates_endpoint_error() -> None:
    def transport(path: str) -> Dict[str, Any]:
        if path.endswith("/stat/device"):
            raise OSError("connection refused")
        return _envelope([{"mac": _CLIENT, "ip": "10.0.0.50", "hostname": "pc50"}])

    out = _adapter(transport).collect()
    assert out.errors  # the device fetch failure is recorded
    assert any(n.hostname == "pc50" for n in out.nodes)  # ...but clients still parsed


def test_collect_never_raises_on_garbage() -> None:
    out = _adapter(lambda path: "not-an-envelope").collect()
    assert out.nodes == ()  # garbage -> nothing, no exception
    assert out.links == ()


def test_collect_ignores_non_dict_rows() -> None:
    def transport(path: str) -> Dict[str, Any]:
        if path.endswith("/stat/device"):
            return _envelope(["nope", 42, None])
        return _envelope([])

    out = _adapter(transport).collect()
    assert out.nodes == ()


def test_collect_no_session_returns_error_not_crash() -> None:
    # No injected transport and no stored credential -> cannot authenticate; the
    # adapter returns empty with an error rather than raising or opening a socket.
    cfg = AdapterConfig(adapter_type="unifi", endpoint="10.0.0.1", credential="uni")
    out = UniFiAdapter(cfg).collect()
    assert out.nodes == ()
    assert out.errors


def test_registered_in_scheduler_builders() -> None:
    from server.netdisco import scheduler

    assert scheduler._ADAPTER_BUILDERS.get("unifi") is UniFiAdapter


def test_no_redirect_handler_blocks_3xx() -> None:
    # The opener installs _NoRedirect so a 3xx Location can never bounce the request
    # to an arbitrary (off-LAN) host -- the SSRF guard. Pin it directly.
    from server.netdisco.adapters.unifi import _NoRedirect

    dropped = _NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://8.8.8.8/")
    assert dropped is None
