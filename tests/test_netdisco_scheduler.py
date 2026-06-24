"""Phase 4: netdisco inventory scheduler cycle (anti-DoS serialized, injectable).

run_inventory_cycle rebuilds the inventory from current snapshots and persists
it; a single _poll_lock serializes cycles so a mashed force-poll (or the loop
firing mid-poll) returns 'busy' instead of launching a second pass.
"""

from __future__ import annotations

from typing import Any

from server.netdisco import scheduler

_SNAP: dict[str, Any] = {
    "device_id": "dev-A",
    "hostname": "PC-A",
    "last_seen": "2026-06-20T10:00:00+00:00",
    "adapters": [
        {
            "mac": "AA-BB-CC-DD-EE-01",
            "ipv4": ["10.0.0.10"],
            "kind": "ethernet",
            "up": True,
            "gateway": "10.0.0.1",
        }
    ],
    "neighbors": [{"ip": "10.0.0.1", "mac": "00-50-56-AA-BB-CC", "state": "reachable"}],
}


def test_run_inventory_cycle_builds_and_persists() -> None:
    captured: list[dict[str, Any]] = []
    result = scheduler.run_inventory_cycle(
        get_snapshots=lambda: [_SNAP],
        upsert=captured.append,
        get_net_devices=lambda: [],
        get_printers=lambda: [],
        set_links=lambda *a: None,
    )
    assert result["busy"] == 0
    # one agent + one agentless gateway endpoint = 2 devices persisted
    assert result["persisted"] == len(captured) == 2
    assert result["linked"] == 0  # no records to link against


def test_run_inventory_cycle_links_identities_by_mac() -> None:
    # The net_devices the inventory just persisted: the reporting agent's own node
    # and a printer node, each carrying the shared MAC of its record. The cycle
    # must FK-link both (agent by MAC -> device_id, printer by MAC -> printer_id).
    amac = "AA-BB-CC-DD-EE-01"  # _SNAP's adapter MAC -> dev-A
    pmac = "11-22-33-44-55-66"
    net_devices = [
        {"device_nid": "nd-mac-" + amac, "mac": amac, "ip": "10.0.0.10"},
        {"device_nid": "nd-mac-" + pmac, "mac": pmac, "ip": "10.0.0.50"},
    ]
    printers = [{"printer_id": "prn-sn-XYZ", "mac": pmac, "ip": "10.0.0.50"}]
    written: list[tuple] = []
    result = scheduler.run_inventory_cycle(
        get_snapshots=lambda: [_SNAP],
        upsert=lambda d: None,
        get_net_devices=lambda: net_devices,
        get_printers=lambda: printers,
        set_links=lambda nid, did, pid: written.append((nid, did, pid)),
    )
    assert result["linked"] == 2
    assert ("nd-mac-" + amac, "dev-A", None) in written
    assert ("nd-mac-" + pmac, None, "prn-sn-XYZ") in written


def test_run_inventory_cycle_returns_busy_when_a_cycle_is_running() -> None:
    scheduler._poll_lock.acquire()
    try:
        result = scheduler.run_inventory_cycle(get_snapshots=lambda: [_SNAP], upsert=lambda d: None)
        assert result["busy"] == 1
        assert result["persisted"] == 0
    finally:
        scheduler._poll_lock.release()


# --- Phase 5: active discovery cycle (scan -> gather -> upsert new only) ---

from server.netdisco.config import NetdiscoConfig  # noqa: E402
from server.netdisco.identity import device_nid  # noqa: E402


def test_run_discovery_cycle_is_noop_when_active_scan_off() -> None:
    cfg = NetdiscoConfig(active_scan=False)

    def boom(_: NetdiscoConfig) -> list[str]:
        raise AssertionError("scan ran while active_scan is off")

    result = scheduler.run_discovery_cycle(
        cfg, scan_fn=boom, get_snapshots=lambda: [], get_known=lambda: [], upsert=lambda d: None
    )
    assert result == {"discovered": 0, "scanned": 0, "active": 0, "busy": 0}


def test_run_discovery_cycle_persists_only_new_scan_hosts() -> None:
    cfg = NetdiscoConfig(active_scan=True)
    captured: list[dict[str, Any]] = []
    result = scheduler.run_discovery_cycle(
        cfg,
        scan_fn=lambda c: ["10.0.0.50"],
        get_snapshots=lambda: [],
        get_known=lambda: [],
        upsert=captured.append,
    )
    assert result["active"] == 1 and result["busy"] == 0
    assert result["scanned"] == 1 and result["discovered"] == 1
    assert len(captured) == 1
    dev = captured[0]
    assert dev["ip"] == "10.0.0.50"
    assert dev["dev_type"] == "unknown"  # scan-only host has no MAC -> UNKNOWN-first
    assert dev["status"] == "discovered"


def test_run_discovery_cycle_skips_known_nids_no_demotion() -> None:
    cfg = NetdiscoConfig(active_scan=True)
    known_nid = device_nid(mac=None, ip="10.0.0.50")  # same nid the scan hit would get
    captured: list[dict[str, Any]] = []
    result = scheduler.run_discovery_cycle(
        cfg,
        scan_fn=lambda c: ["10.0.0.50"],
        get_snapshots=lambda: [],
        get_known=lambda: [{"device_nid": known_nid, "dev_type": "router"}],
        upsert=captured.append,
    )
    assert result["discovered"] == 0
    assert captured == []  # a known device is never re-upserted (no router->endpoint demotion)


def test_run_discovery_cycle_returns_busy_when_locked() -> None:
    cfg = NetdiscoConfig(active_scan=True)
    scheduler._poll_lock.acquire()
    try:
        result = scheduler.run_discovery_cycle(
            cfg, scan_fn=lambda c: ["10.0.0.50"], get_snapshots=lambda: [], get_known=lambda: []
        )
        assert result["busy"] == 1 and result["discovered"] == 0
    finally:
        scheduler._poll_lock.release()


def test_run_discovery_cycle_harvests_arp_and_routes_from_infra_only() -> None:
    cfg = NetdiscoConfig(active_scan=True)
    captured: list[dict[str, Any]] = []
    sessions_for: list[str] = []
    known = [
        {"device_nid": "rtr", "ip": "10.0.0.1", "dev_type": "router"},
        {"device_nid": "ep", "ip": "10.0.0.2", "dev_type": "endpoint"},  # not harvested
    ]
    result = scheduler.run_discovery_cycle(
        cfg,
        scan_fn=lambda c: [],
        get_snapshots=lambda: [],
        get_known=lambda: known,
        session_factory=lambda ip, c: sessions_for.append(ip) or ("sess", ip),
        harvest_arp_fn=lambda s: [("10.0.0.50", "AA-BB-CC-00-00-50")],
        harvest_routes_fn=lambda s: [("10.0.0.0/24", "10.0.0.99", 1)],
        upsert=captured.append,
    )
    assert sessions_for == ["10.0.0.1"]  # only the router was harvested, not the endpoint
    ips = {d["ip"] for d in captured}
    assert "10.0.0.50" in ips and "10.0.0.99" in ips  # ARP neighbour + route next-hop found
    assert result["active"] == 1 and result["busy"] == 0


# --- Phase 6: classify cycle (probe known -> classify -> upsert type + ifaces) ---

from server.analytics.oui import normalize_mac  # noqa: E402
from server.netdisco.models import DeviceProfile, NetInterface  # noqa: E402


def _router_profile(ip: str = "10.0.0.1") -> DeviceProfile:
    return DeviceProfile(
        ip=ip,
        responded=True,
        ip_forwarding=True,
        sys_descr="RouterOS 7",
        sys_object_id="1.3.6.1.4.1.14988.1",
        interfaces=(NetInterface(if_index=1, name="ether1", if_type=6),),
    )


def test_classify_cycle_is_noop_when_disabled() -> None:
    cfg = NetdiscoConfig(enabled=False)

    def boom(_ip: str, _sess: object) -> DeviceProfile:
        raise AssertionError("probed while netdisco disabled")

    result = scheduler.run_classify_cycle(
        cfg,
        get_known=lambda: [{"device_nid": "n", "ip": "10.0.0.1", "dev_type": "endpoint"}],
        get_agent_macs=lambda: set(),
        probe_fn=boom,
    )
    assert result == {"classified": 0, "probed": 0, "busy": 0}


def test_classify_cycle_probes_unclassified_and_sets_type() -> None:
    cfg = NetdiscoConfig(enabled=True)
    ups: list[dict[str, Any]] = []
    ifaces: list[tuple[str, list]] = []
    result = scheduler.run_classify_cycle(
        cfg,
        get_known=lambda: [
            {"device_nid": "n1", "ip": "10.0.0.1", "dev_type": "endpoint", "status": "discovered"}
        ],
        get_agent_macs=lambda: set(),
        probe_fn=lambda ip, sess: _router_profile(ip),
        session_factory=lambda ip, c: object(),
        upsert=ups.append,
        store_interfaces=lambda nid, rows: ifaces.append((nid, rows)),
    )
    assert result == {"classified": 1, "probed": 1, "busy": 0}
    assert ups[0]["device_nid"] == "n1" and ups[0]["dev_type"] == "router"
    assert ups[0]["status"] == "up" and ups[0]["model"] == "RouterOS 7"
    assert ifaces[0][0] == "n1" and len(ifaces[0][1]) == 1


def test_classify_cycle_skips_already_classified_infra() -> None:
    cfg = NetdiscoConfig(enabled=True)

    def boom(_ip: str, _sess: object) -> DeviceProfile:
        raise AssertionError("re-probed an already-classified switch")

    result = scheduler.run_classify_cycle(
        cfg,
        get_known=lambda: [{"device_nid": "s", "ip": "10.0.0.2", "dev_type": "switch"}],
        get_agent_macs=lambda: set(),
        probe_fn=boom,
    )
    assert result["probed"] == 0 and result["classified"] == 0


def test_classify_cycle_skips_our_own_agents() -> None:
    cfg = NetdiscoConfig(enabled=True)
    amac = normalize_mac("aa:bb:cc:dd:ee:ff")

    def boom(_ip: str, _sess: object) -> DeviceProfile:
        raise AssertionError("probed our own agent machine")

    result = scheduler.run_classify_cycle(
        cfg,
        get_known=lambda: [
            {
                "device_nid": "a",
                "ip": "10.0.0.3",
                "dev_type": "endpoint",
                "mac": "AA:BB:CC:DD:EE:FF",
            }
        ],
        get_agent_macs=lambda: {amac},
        probe_fn=boom,
    )
    assert result["probed"] == 0


def test_classify_cycle_skips_non_rfc1918() -> None:
    cfg = NetdiscoConfig(enabled=True)

    def boom(_ip: str, _sess: object) -> DeviceProfile:
        raise AssertionError("probed a public IP")

    result = scheduler.run_classify_cycle(
        cfg,
        get_known=lambda: [{"device_nid": "p", "ip": "8.8.8.8", "dev_type": "endpoint"}],
        get_agent_macs=lambda: set(),
        probe_fn=boom,
    )
    assert result["probed"] == 0


def test_classify_cycle_silent_host_is_endpoint_via_inventory_mac() -> None:
    cfg = NetdiscoConfig(enabled=True)
    ups: list[dict[str, Any]] = []
    result = scheduler.run_classify_cycle(
        cfg,
        get_known=lambda: [
            {"device_nid": "e", "ip": "10.0.0.4", "dev_type": "unknown", "mac": "00:1b:44:11:3a:b7"}
        ],
        get_agent_macs=lambda: set(),
        probe_fn=lambda ip, sess: DeviceProfile(ip=ip, responded=False),
        session_factory=lambda ip, c: object(),
        upsert=ups.append,
        store_interfaces=lambda nid, rows: None,
    )
    assert result["classified"] == 1
    assert ups[0]["dev_type"] == "endpoint"  # silent, but seen on the LAN (inventory MAC)


def test_classify_cycle_returns_busy_when_locked() -> None:
    cfg = NetdiscoConfig(enabled=True)
    scheduler._poll_lock.acquire()
    try:
        result = scheduler.run_classify_cycle(
            cfg,
            get_known=lambda: [{"device_nid": "n", "ip": "10.0.0.1", "dev_type": "endpoint"}],
            get_agent_macs=lambda: set(),
            probe_fn=lambda ip, sess: _router_profile(),
        )
        assert result["busy"] == 1 and result["classified"] == 0
    finally:
        scheduler._poll_lock.release()
