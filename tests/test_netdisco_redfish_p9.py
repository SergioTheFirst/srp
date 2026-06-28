"""Ф9c: Redfish BMC adapter (server serial/model + OOB management MAC/IP).

Read-only, fail-soft: ``collect()`` must never raise. A Redfish service exposes a
nested tree: ``/redfish/v1/Systems`` (a collection whose members carry Model /
SerialNumber / Manufacturer) and ``/redfish/v1/Managers/<id>/EthernetInterfaces``
(the BMC out-of-band NICs carrying the OOB MAC + management IP). The adapter walks
that tree, attaches the (single) system's identity to each OOB interface, and emits
one server identity hint per OOB MAC. Every hop is bounded (per-collection cap +
global request budget), a public IP is dropped (RFC1918 only), a hostile
``@odata.id`` is rejected (no SSRF/path-escape), and a failure on one endpoint is
isolated in ``errors``. The HTTP transport is injected so the suite never opens a
socket. RED first.
"""

from __future__ import annotations

from typing import Any, Dict, List

from server.analytics.oui import normalize_mac
from server.netdisco.adapters.base import AdapterConfig
from server.netdisco.adapters.redfish import _MAX_MEMBERS, RedfishAdapter

_OOB_MAC = "aa:bb:cc:00:00:99"

_TREE: Dict[str, Any] = {
    "/redfish/v1/Systems": {"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]},
    "/redfish/v1/Systems/1": {
        "Model": "PowerEdge R740",
        "SerialNumber": "SVC123",
        "Manufacturer": "Dell Inc.",
        "HostName": "esx01",
    },
    "/redfish/v1/Managers": {"Members": [{"@odata.id": "/redfish/v1/Managers/iDRAC.1"}]},
    "/redfish/v1/Managers/iDRAC.1": {
        "EthernetInterfaces": {"@odata.id": "/redfish/v1/Managers/iDRAC.1/EthernetInterfaces"}
    },
    "/redfish/v1/Managers/iDRAC.1/EthernetInterfaces": {
        "Members": [{"@odata.id": "/redfish/v1/Managers/iDRAC.1/EthernetInterfaces/NIC.1"}]
    },
    "/redfish/v1/Managers/iDRAC.1/EthernetInterfaces/NIC.1": {
        "MACAddress": _OOB_MAC,
        "IPv4Addresses": [{"Address": "10.0.0.99"}],
    },
}


def _adapter(transport) -> RedfishAdapter:
    cfg = AdapterConfig(adapter_type="redfish", endpoint="10.0.0.99", credential="bmc")
    return RedfishAdapter(cfg, transport=transport)


def _tree_transport(tree: Dict[str, Any]):
    def transport(path: str) -> Any:
        if path not in tree:
            return {}
        return tree[path]

    return transport


def test_collect_yields_server_identity_for_oob_mac() -> None:
    out = _adapter(_tree_transport(_TREE)).collect()
    assert out.errors == ()
    assert len(out.nodes) == 1
    node = out.nodes[0]
    assert normalize_mac(node.mac) == normalize_mac(_OOB_MAC)
    assert node.ip == "10.0.0.99"
    assert node.model == "PowerEdge R740"
    assert node.serial == "SVC123"
    assert node.vendor == "Dell Inc."
    assert node.hostname == "esx01"
    assert node.dev_type == "server"


def test_collect_drops_public_management_ip() -> None:
    tree = dict(_TREE)
    tree["/redfish/v1/Managers/iDRAC.1/EthernetInterfaces/NIC.1"] = {
        "MACAddress": _OOB_MAC,
        "IPv4Addresses": [{"Address": "8.8.8.8"}],
    }
    out = _adapter(_tree_transport(tree)).collect()
    assert len(out.nodes) == 1  # MAC still gives identity...
    assert out.nodes[0].ip is None  # ...but a public mgmt IP never enters net_*


def test_collect_sanitises_hostname() -> None:
    tree = dict(_TREE)
    tree["/redfish/v1/Systems/1"] = {
        "Model": "X",
        "SerialNumber": "Y",
        "Manufacturer": "Z",
        "HostName": "bad\x00name",
    }
    out = _adapter(_tree_transport(tree)).collect()
    assert all(n.hostname != "bad\x00name" for n in out.nodes)
    assert out.nodes[0].hostname is None


def test_collect_isolates_systems_failure_but_still_emits_oob_mac() -> None:
    def transport(path: str) -> Any:
        if path == "/redfish/v1/Systems":
            raise OSError("connection refused")
        return _TREE.get(path, {})

    out = _adapter(transport).collect()
    assert out.errors  # the Systems fetch failure is recorded
    assert len(out.nodes) == 1  # ...but the OOB MAC is still discovered
    node = out.nodes[0]
    assert normalize_mac(node.mac) == normalize_mac(_OOB_MAC)
    assert node.serial is None  # no system -> no attributed serial (UNKNOWN over guess)
    assert node.model is None


def test_multi_system_does_not_attribute_serial() -> None:
    tree = dict(_TREE)
    tree["/redfish/v1/Systems"] = {
        "Members": [
            {"@odata.id": "/redfish/v1/Systems/1"},
            {"@odata.id": "/redfish/v1/Systems/2"},
        ]
    }
    tree["/redfish/v1/Systems/2"] = {
        "Model": "OtherBlade",
        "SerialNumber": "SVC999",
        "Manufacturer": "Dell Inc.",
        "HostName": "esx02",
    }
    out = _adapter(_tree_transport(tree)).collect()
    # Two systems share one BMC -> attributing one blade's serial to the shared OOB
    # NIC would be a false identity. Leave model/serial empty (honest UNKNOWN).
    assert out.nodes[0].serial is None
    assert out.nodes[0].model is None


def test_collect_never_raises_on_garbage() -> None:
    out = _adapter(lambda path: "not-a-dict").collect()
    assert out.nodes == ()
    assert out.links == ()


def test_collect_ignores_non_dict_members() -> None:
    tree = dict(_TREE)
    tree["/redfish/v1/Managers"] = {"Members": ["nope", 42, None]}
    out = _adapter(_tree_transport(tree)).collect()
    assert out.nodes == ()  # no usable manager -> no OOB MAC


def test_collect_rejects_unsafe_odata_id() -> None:
    requested: List[str] = []

    def transport(path: str) -> Any:
        requested.append(path)
        if path == "/redfish/v1/Managers":
            return {
                "Members": [
                    {"@odata.id": "http://evil.example/redfish"},
                    {"@odata.id": "/redfish/v1/../../etc/passwd"},
                    {"@odata.id": "/redfish/v1/Managers/iDRAC.1"},
                ]
            }
        return _TREE.get(path, {})

    out = _adapter(transport).collect()
    assert "http://evil.example/redfish" not in requested  # absolute URL rejected
    assert not any(".." in p for p in requested)  # path-escape rejected
    assert len(out.nodes) == 1  # the one safe manager still resolves the OOB MAC


def test_collect_caps_collection_members() -> None:
    big = {"Members": [{"@odata.id": f"/redfish/v1/Systems/{i}"} for i in range(500)]}
    tree = dict(_TREE)
    tree["/redfish/v1/Systems"] = big
    fetched: List[str] = []

    def transport(path: str) -> Any:
        fetched.append(path)
        if path.startswith("/redfish/v1/Systems/"):
            return {"Model": "m", "SerialNumber": "s", "Manufacturer": "v"}
        return tree.get(path, {})

    _adapter(transport).collect()
    system_detail_calls = [p for p in fetched if p.startswith("/redfish/v1/Systems/")]
    assert len(system_detail_calls) <= _MAX_MEMBERS  # a flood can't fan out unbounded


def test_collect_no_session_returns_error_not_crash() -> None:
    # No injected transport and no stored credential -> cannot authenticate; the
    # adapter returns empty with an error rather than raising or opening a socket.
    cfg = AdapterConfig(adapter_type="redfish", endpoint="10.0.0.99", credential="bmc")
    out = RedfishAdapter(cfg).collect()
    assert out.nodes == ()
    assert out.errors


def test_registered_in_scheduler_builders() -> None:
    from server.netdisco import scheduler

    assert scheduler._ADAPTER_BUILDERS.get("redfish") is RedfishAdapter


def test_no_redirect_handler_blocks_3xx() -> None:
    # The opener installs _NoRedirect so a 3xx Location can never bounce the request
    # to an arbitrary (off-LAN) host -- the SSRF guard. Pin it directly.
    from server.netdisco.adapters.redfish import _NoRedirect

    dropped = _NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://8.8.8.8/")
    assert dropped is None


def test_merge_enriches_known_device_by_mac() -> None:
    # End-to-end: a Redfish OOB MAC that matches an existing net_device enriches its
    # empty identity (serial/model via the Ф8 fill-empty writer), never overriding.
    from server.netdisco.adapter_merge import merge_adapter_result

    out = _adapter(_tree_transport(_TREE)).collect()
    known = [{"device_nid": "nd-mac-aabbcc000099", "mac": _OOB_MAC}]
    calls: List[dict] = []

    def fill(nid: str, **kwargs: Any) -> None:
        calls.append({"nid": nid, **kwargs})

    def upsert(row: dict, now: Any = None) -> None:  # pragma: no cover - must not run
        raise AssertionError("known MAC must enrich, never add")

    stats = merge_adapter_result(out, known, fill=fill, upsert=upsert)
    assert stats == {"enriched": 1, "added": 0}
    assert calls[0]["nid"] == "nd-mac-aabbcc000099"
    assert calls[0]["model"] == "PowerEdge R740"
