"""Phase 4: netdisco inventory scheduler cycle (anti-DoS serialized, injectable).

run_inventory_cycle rebuilds the inventory from current snapshots and persists
it; a single _poll_lock serializes cycles so a mashed force-poll (or the loop
firing mid-poll) returns 'busy' instead of launching a second pass.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from server.netdisco import oids, passive, scheduler

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


# --- T1: agent-reported routing table -> net_routes, hooked into the same cycle ---


def test_run_inventory_cycle_persists_agent_routes() -> None:
    calls: list[list[dict[str, Any]]] = []

    def _persist_routes(snapshots: list[dict[str, Any]]) -> int:
        calls.append(snapshots)
        return 3

    result = scheduler.run_inventory_cycle(
        get_snapshots=lambda: [_SNAP],
        upsert=lambda d: None,
        get_net_devices=lambda: [],
        get_printers=lambda: [],
        set_links=lambda *a: None,
        persist_routes=_persist_routes,
    )
    assert result["routes"] == 3
    assert result["busy"] == 0
    assert calls == [[_SNAP]]  # invoked with the SAME snapshots the inventory was built from


def test_run_inventory_cycle_route_persist_failure_does_not_break_cycle() -> None:
    def _boom(snapshots: list[dict[str, Any]]) -> int:
        raise RuntimeError("route persist blew up")

    result = scheduler.run_inventory_cycle(
        get_snapshots=lambda: [_SNAP],
        upsert=lambda d: None,
        get_net_devices=lambda: [],
        get_printers=lambda: [],
        set_links=lambda *a: None,
        persist_routes=_boom,
    )
    assert result["routes"] == 0  # swallowed, not propagated
    assert result["busy"] == 0
    assert result["persisted"] == 2  # the inventory persist step still ran to completion


def test_run_inventory_cycle_returns_zero_routes_when_locked() -> None:
    scheduler._poll_lock.acquire()
    try:
        result = scheduler.run_inventory_cycle(get_snapshots=lambda: [_SNAP], upsert=lambda d: None)
        assert result["routes"] == 0
        assert result["hints"] == 0
        assert result["busy"] == 1
    finally:
        scheduler._poll_lock.release()


# --- P1: relayed lan_hints (mDNS/SSDP/WSD) folded into the passive fill -----


def test_run_inventory_cycle_applies_relayed_lan_hints() -> None:
    filled: list[tuple[str, dict[str, Any]]] = []

    def _collect_hints(snapshots: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        assert snapshots == [_SNAP]  # invoked with the SAME snapshots the inventory was built from
        return {
            "mdns": {
                "10.0.0.30": passive.PassiveHint(
                    ip="10.0.0.30", source="mdns", hostname="PRINTER-1"
                )
            }
        }

    result = scheduler.run_inventory_cycle(
        get_snapshots=lambda: [_SNAP],
        upsert=lambda d: None,
        get_net_devices=lambda: [{"ip": "10.0.0.30", "device_nid": "nd-x"}],
        get_printers=lambda: [],
        set_links=lambda *a: None,
        collect_lan_hints=_collect_hints,
        fill_hints=lambda nid, **kw: filled.append((nid, kw)),
    )
    assert result["hints"] == 1
    assert result["busy"] == 0
    assert filled == [("nd-x", {"hostname": "PRINTER-1"})]


def test_run_inventory_cycle_lan_hints_ignored_when_ip_not_known() -> None:
    filled: list[tuple[str, dict[str, Any]]] = []

    def _collect_hints(snapshots: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {
            "mdns": {
                "10.0.0.99": passive.PassiveHint(ip="10.0.0.99", source="mdns", hostname="GHOST")
            }
        }

    result = scheduler.run_inventory_cycle(
        get_snapshots=lambda: [_SNAP],
        upsert=lambda d: None,
        get_net_devices=lambda: [{"ip": "10.0.0.30", "device_nid": "nd-x"}],  # different ip
        get_printers=lambda: [],
        set_links=lambda *a: None,
        collect_lan_hints=_collect_hints,
        fill_hints=lambda nid, **kw: filled.append((nid, kw)),
    )
    assert result["hints"] == 0
    assert filled == []


def test_run_inventory_cycle_lan_hints_failure_does_not_break_cycle() -> None:
    def _boom(snapshots: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        raise RuntimeError("lan hint relay blew up")

    result = scheduler.run_inventory_cycle(
        get_snapshots=lambda: [_SNAP],
        upsert=lambda d: None,
        get_net_devices=lambda: [],
        get_printers=lambda: [],
        set_links=lambda *a: None,
        collect_lan_hints=_boom,
    )
    assert result["hints"] == 0  # swallowed, not propagated
    assert result["busy"] == 0
    assert result["persisted"] == 2  # the inventory persist step still ran to completion


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
        scan_fn=lambda c, **_: ["10.0.0.50"],
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
        scan_fn=lambda c, **_: ["10.0.0.50"],
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
        scan_fn=lambda c, **_: [],
        get_snapshots=lambda: [],
        get_known=lambda: known,
        session_factory=lambda ip, c: sessions_for.append(ip) or ("sess", ip),
        harvest_arp_fn=lambda s: [("10.0.0.50", "AA-BB-CC-00-00-50")],
        harvest_routes_fn=lambda s: [("10.0.0.0/24", "10.0.0.99", 1)],
        upsert=captured.append,
        add_route=lambda *a: None,
    )
    assert sessions_for == ["10.0.0.1"]  # only the router was harvested, not the endpoint
    ips = {d["ip"] for d in captured}
    assert "10.0.0.50" in ips and "10.0.0.99" in ips  # ARP neighbour + route next-hop found
    assert result["active"] == 1 and result["busy"] == 0


def test_run_discovery_cycle_surfaces_a_saturated_scan(monkeypatch) -> None:
    """F6/MEDIUM-2: a saturated /24 dropped inside ``scan_fn`` must be visible to
    the operator, not just a log line -- ``result["saturated"]`` counts the
    dropped ranges and a single ``scan_saturated`` journal row is written."""
    cfg = NetdiscoConfig(active_scan=True)

    def fake_scan(c, *, on_saturated=None, **_):
        if on_saturated is not None:
            on_saturated(["10.33.0"])
        return []

    changes: list[tuple] = []
    result = scheduler.run_discovery_cycle(
        cfg,
        scan_fn=fake_scan,
        get_snapshots=lambda: [],
        get_known=lambda: [],
        upsert=lambda d: None,
        store_change=lambda *a: changes.append(a),
    )
    assert result["saturated"] == 1
    assert len(changes) == 1
    kind, device_nid, detail = changes[0][:3]
    assert kind == "scan_saturated"
    assert device_nid is None
    assert detail == {"ranges": ["10.33.0"]}


def test_run_discovery_cycle_reports_zero_saturated_when_nothing_dropped() -> None:
    cfg = NetdiscoConfig(active_scan=True)
    changes: list[tuple] = []
    result = scheduler.run_discovery_cycle(
        cfg,
        scan_fn=lambda c, **_: [],
        get_snapshots=lambda: [],
        get_known=lambda: [],
        upsert=lambda d: None,
        store_change=lambda *a: changes.append(a),
    )
    assert result["saturated"] == 0
    assert changes == []


def test_harvest_infra_persists_routes_and_seeds_lldp_mgmt() -> None:
    # A2: the (cidr, next_hop, ifindex) triple is persisted (not just next_hop kept).
    # A1: LLDP remote management addresses become discovery candidates (no ping scan).
    written: list = []
    pairs = scheduler._harvest_infra(
        [{"device_nid": "nd-rt", "dev_type": "router", "ip": "10.0.0.1"}],
        NetdiscoConfig(),
        session_factory=lambda ip, c: "sess",
        harvest_arp_fn=lambda s: [],
        harvest_routes_fn=lambda s: [("10.1.0.0/24", "10.0.0.99", 3)],
        collect_mgmt_fn=lambda local, s: [("nd-rt", "10.0.0.50")],
        add_route=lambda nid, cidr, nh, ifx: written.append((nid, cidr, nh, ifx)),
    )
    assert ("10.0.0.99", None) in pairs  # route next-hop -> candidate (kept)
    assert ("10.0.0.50", None) in pairs  # lldp mgmt-addr -> candidate (A1)
    assert written == [("nd-rt", "10.1.0.0/24", "10.0.0.99", 3)]  # route persisted (A2)


def test_harvest_infra_mgmt_failure_never_breaks_the_cycle() -> None:
    # a bad infra host (mgmt walk raises) must not lose the arp/route candidates
    def boom(local: str, s: object) -> list:
        raise RuntimeError("snmp blew up")

    pairs = scheduler._harvest_infra(
        [{"device_nid": "nd-rt", "dev_type": "router", "ip": "10.0.0.1"}],
        NetdiscoConfig(),
        session_factory=lambda ip, c: "sess",
        harvest_arp_fn=lambda s: [("10.0.0.7", "AA-BB-CC-00-00-07")],
        harvest_routes_fn=lambda s: [],
        collect_mgmt_fn=boom,
    )
    assert ("10.0.0.7", "AA-BB-CC-00-00-07") in pairs


def test_net_routes_roundtrip_upsert(tmp_path: Path) -> None:
    from server import db

    db.init_db(tmp_path / "routes.db")
    db.add_net_route("nd-rt", "10.1.0.0/24", "10.0.0.99", 3)
    db.add_net_route("nd-rt", "10.1.0.0/24", "10.0.0.99", 3)  # idempotent upsert
    rows = [r for r in db.get_net_routes() if r["device_nid"] == "nd-rt"]
    assert len(rows) == 1
    assert rows[0]["cidr"] == "10.1.0.0/24" and rows[0]["next_hop"] == "10.0.0.99"
    assert rows[0]["ifindex"] == 3


# --- Phase 6: classify cycle (probe known -> classify -> upsert type + ifaces) ---

from server.analytics.oui import normalize_mac  # noqa: E402
from server.netdisco.models import DeviceProfile, NetInterface  # noqa: E402


def _router_profile(ip: str = "10.0.0.1") -> DeviceProfile:
    # o5-A2 conscious contract change: classify() now requires sysServices L3 +
    # >=2 Ethernet ports to corroborate ip_forwarding before calling it a router
    # (a bare ipForwarding=1 bit is also set by Docker/Hyper-V/ICS hosts) -- this
    # fixture was updated to supply that corroboration so it still yields "router".
    return DeviceProfile(
        ip=ip,
        responded=True,
        ip_forwarding=True,
        sys_services=oids.SYS_SERVICES_L3,
        sys_descr="RouterOS 7",
        sys_object_id="1.3.6.1.4.1.14988.1",
        interfaces=(
            NetInterface(if_index=1, name="ether1", if_type=6),
            NetInterface(if_index=2, name="ether2", if_type=6),
        ),
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
        set_mute=lambda *a: None,
    )
    assert result == {"classified": 1, "probed": 1, "busy": 0}
    assert ups[0]["device_nid"] == "n1" and ups[0]["dev_type"] == "router"
    assert ups[0]["status"] == "up" and ups[0]["model"] == "RouterOS 7"
    # o5-A2: _router_profile now carries 2 interfaces (router-corroboration signal).
    assert ifaces[0][0] == "n1" and len(ifaces[0][1]) == 2


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
        set_mute=lambda *a: None,
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


def test_classify_resolves_community_once_per_cycle(monkeypatch) -> None:
    # o5-B4: default_store() is DPAPI/file I/O -- it must be paid once per cycle,
    # not once per probed host (session_factory keeps its plain 2-arg shape here,
    # matching every other stub in this file).
    calls: list[int] = []

    def counting_store():
        calls.append(1)
        return None

    monkeypatch.setattr(scheduler, "default_store", counting_store)
    cfg = NetdiscoConfig(enabled=True)
    devices = [
        {"device_nid": f"n{i}", "ip": f"10.0.0.{i}", "dev_type": "endpoint", "status": "discovered"}
        for i in range(1, 6)
    ]
    result = scheduler.run_classify_cycle(
        cfg,
        get_known=lambda: devices,
        get_agent_macs=lambda: set(),
        probe_fn=lambda ip, sess: DeviceProfile(ip=ip, responded=False),
        upsert=lambda d: None,
        store_interfaces=lambda nid, rows: None,
        set_mute=lambda *a: None,
    )
    assert result["classified"] == 5
    assert len(calls) == 1


# --- o5-B6: SNMP negative cache for silent hosts + parallel probe fan-out -------


def test_classify_skips_recently_mute_hosts() -> None:
    cfg = NetdiscoConfig(enabled=True)
    future = "2099-01-01T00:00:00+00:00"

    def boom(_ip: str, _sess: object) -> DeviceProfile:
        raise AssertionError("probed a still-muted host")

    result = scheduler.run_classify_cycle(
        cfg,
        get_known=lambda: [
            {
                "device_nid": "m1",
                "ip": "10.0.0.9",
                "dev_type": "unknown",
                "snmp_mute_until": future,
            }
        ],
        get_agent_macs=lambda: set(),
        probe_fn=boom,
    )
    assert result["probed"] == 0


def test_classify_marks_mute_host() -> None:
    cfg = NetdiscoConfig(enabled=True)
    muted: list[tuple] = []
    result = scheduler.run_classify_cycle(
        cfg,
        get_known=lambda: [{"device_nid": "e2", "ip": "10.0.0.5", "dev_type": "unknown"}],
        get_agent_macs=lambda: set(),
        probe_fn=lambda ip, sess: DeviceProfile(ip=ip, responded=False),
        session_factory=lambda ip, c: object(),
        upsert=lambda d: None,
        store_interfaces=lambda nid, rows: None,
        set_mute=lambda nid, until: muted.append((nid, until)),
    )
    assert result["classified"] == 1
    assert len(muted) == 1
    assert muted[0][0] == "e2"
    assert muted[0][1]  # non-empty deadline (now + 24h), not a clear-to-None


def test_classify_writes_db_from_main_thread() -> None:
    cfg = NetdiscoConfig(enabled=True)
    main_thread_id = threading.get_ident()
    thread_ids: set = set()
    devices = [
        {"device_nid": f"t{i}", "ip": f"10.0.0.{i}", "dev_type": "unknown"} for i in range(1, 6)
    ]

    def capture_upsert(d: dict) -> None:
        thread_ids.add(threading.get_ident())

    result = scheduler.run_classify_cycle(
        cfg,
        get_known=lambda: devices,
        get_agent_macs=lambda: set(),
        probe_fn=lambda ip, sess: DeviceProfile(ip=ip, responded=False),
        session_factory=lambda ip, c: object(),
        upsert=capture_upsert,
        store_interfaces=lambda nid, rows: None,
        set_mute=lambda *a: None,
    )
    assert result["classified"] == 5
    assert thread_ids == {main_thread_id}


def test_classify_probe_exception_is_not_muted_or_upserted() -> None:
    """F2: a probe crash is not the same fact as "silent host" -- muting it would
    hide the real cause for 24h, and worse, upserting a demoted verdict off a
    ``responded=False`` profile could downgrade an already-known infra device
    (``upsert_net_device``'s downgrade-guard only protects 'unknown'). The crashed
    target must be retried next cycle instead: no upsert, no mute, not counted in
    probed/classified -- while the healthy target next to it is still classified."""
    cfg = NetdiscoConfig(enabled=True)
    muted: list[tuple] = []
    ups: list[dict[str, Any]] = []

    def flaky_probe(ip: str, sess: object) -> DeviceProfile:
        if ip == "10.0.0.9":
            raise OSError("socket exhaustion")
        return DeviceProfile(ip=ip, responded=True)

    result = scheduler.run_classify_cycle(
        cfg,
        get_known=lambda: [
            {"device_nid": "bad", "ip": "10.0.0.9", "dev_type": "unknown"},
            {"device_nid": "good", "ip": "10.0.0.10", "dev_type": "unknown"},
        ],
        get_agent_macs=lambda: set(),
        probe_fn=flaky_probe,
        session_factory=lambda ip, c: object(),
        upsert=ups.append,
        store_interfaces=lambda nid, rows: None,
        set_mute=lambda nid, until: muted.append((nid, until)),
    )
    assert result["probed"] == 1 and result["classified"] == 1  # crashed target not counted
    assert all(nid != "bad" for nid, _until in muted)  # never muted
    assert all(u["device_nid"] != "bad" for u in ups)  # never upserted
    assert any(u["device_nid"] == "good" for u in ups)  # healthy target still processed
