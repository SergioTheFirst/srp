"""Phase 9 -- §4.5 topology reconcile cycle (RED first).

``run_topology_cycle`` collects link evidence off the known infra devices, fuses it
into a graph, and persists ``net_links`` + a ``net_topology_snapshots`` row. It is
gated by ``cfg.enabled``, serialized by the shared poll lock (busy-return), only
probes RFC1918 infra, and is idempotent -- a rerun must not duplicate links.
Dependencies are injected so the cycle runs without real SNMP / DB.
"""

from __future__ import annotations

from pathlib import Path

import server.db as db
from server.analytics.oui import normalize_mac
from server.netdisco import reconcile
from server.netdisco.config import NetdiscoConfig
from server.netdisco.evidence import HIGH, SOURCE_FDB_EDGE, SOURCE_LLDP, LinkEvidence

HOST_AA = normalize_mac("00:00:00:00:00:aa")


def _switch(nid="nd-chassis-sw1", ip="10.0.0.1"):
    return {"device_nid": nid, "ip": ip, "dev_type": "switch", "mac": "00-11-22-33-44-55"}


def _fake_collect(device, session, *, infra_macs=frozenset(), radio_ifindexes=frozenset()):
    return [LinkEvidence(device.nid, HOST_AA, SOURCE_FDB_EDGE, HIGH, 3)]


class _NoOidSession:
    """A6: minimal fake session for probes that don't need a live SNMP response --
    just enough surface (a no-answer ``get``) for reconcile's sys_object_id
    GET-when-empty fallback to run without crashing on a plain ``object()``."""

    def get(self, oid_list):
        return {}

    def walk(self, base_oid, *, max_rows=512):
        return {}


def test_cycle_is_a_noop_when_disabled():
    res = reconcile.run_topology_cycle(NetdiscoConfig(enabled=False), get_known=lambda: [_switch()])
    assert res == {"links": 0, "probed": 0, "busy": 0}


def test_cycle_returns_busy_when_lock_held():
    from server.netdisco import scheduler

    assert scheduler._poll_lock.acquire(blocking=False)
    try:
        res = reconcile.run_topology_cycle(
            NetdiscoConfig(enabled=True), get_known=lambda: [_switch()]
        )
        assert res["busy"] == 1 and res["links"] == 0
    finally:
        scheduler._poll_lock.release()


def test_cycle_persists_links_and_snapshot_for_probed_infra_only():
    devices = [_switch(), {"device_nid": "nd-mac-end", "ip": "10.0.0.9", "dev_type": "endpoint"}]
    captured: dict = {}
    res = reconcile.run_topology_cycle(
        NetdiscoConfig(enabled=True),
        get_known=lambda: devices,
        get_device=lambda nid: None,
        get_interfaces=lambda: [],
        get_iface_macs=lambda: {},
        session_factory=lambda ip, cfg: _NoOidSession(),
        collect=_fake_collect,
        collect_med_fn=lambda *a, **k: {},
        replace_links=lambda rows, nodes, received_at=None: captured.update(rows=rows, nodes=nodes),
        store_snapshot=lambda graph, received_at=None: captured.update(graph=graph),
        upsert=lambda dev, received_at=None: captured.setdefault("upserts", []).append(dev),
        get_prev_snapshot=lambda: None,
        store_change=lambda *a, **k: None,
        set_status=lambda *a, **k: None,
    )
    assert res["probed"] == 1 and res["links"] == 1 and res["busy"] == 0
    assert captured["nodes"] == {"nd-chassis-sw1"}  # endpoint never probed
    assert captured["rows"][0]["a_nid"] == "nd-chassis-sw1"
    assert captured["rows"][0]["b_nid"] == "nd-mac-" + HOST_AA
    assert len(captured["graph"]["links"]) == 1
    assert [u["device_nid"] for u in captured["upserts"]] == ["nd-chassis-sw1"]


def test_link_row_carries_local_ifindex():
    # o5-A10: a fused SOURCE_FDB_EDGE's local_if must survive into the row handed
    # to replace_links (as a_if) so the unified assembler can later resolve link
    # physics (speed_mbps/port_down) from net_interfaces by ifIndex.
    def collect(device, session, *, infra_macs=frozenset(), radio_ifindexes=frozenset()):
        return [LinkEvidence(device.nid, HOST_AA, SOURCE_FDB_EDGE, HIGH, 7)]

    captured: dict = {}
    reconcile.run_topology_cycle(
        NetdiscoConfig(enabled=True),
        get_known=lambda: [_switch()],
        get_device=lambda nid: None,
        get_interfaces=lambda: [],
        get_iface_macs=lambda: {},
        session_factory=lambda ip, cfg: _NoOidSession(),
        collect=collect,
        collect_med_fn=lambda *a, **k: {},
        replace_links=lambda rows, nodes, received_at=None: captured.update(rows=rows),
        store_snapshot=lambda *a, **k: None,
        upsert=lambda *a, **k: None,
        get_prev_snapshot=lambda: None,
        store_change=lambda *a, **k: None,
        set_status=lambda *a, **k: None,
    )
    assert captured["rows"][0]["a_if"] == 7


def test_cycle_does_not_upsert_a_probed_device_with_no_evidence():
    # D2/KodSR L5: last_seen must only advance on real evidence -- a probed but
    # silent (SNMP-mute/timeout) infra device must not get upserted at all this
    # cycle, or its last_seen would "freshen" on every silent pass.
    captured: dict = {"upserts": []}
    reconcile.run_topology_cycle(
        NetdiscoConfig(enabled=True),
        get_known=lambda: [_switch()],
        get_device=lambda nid: None,
        get_interfaces=lambda: [],
        get_iface_macs=lambda: {},
        session_factory=lambda ip, cfg: _NoOidSession(),
        collect=lambda *a, **k: [],  # no LLDP/CDP/FDB evidence
        collect_wireless_fn=lambda *a, **k: [],
        collect_med_fn=lambda *a, **k: {},
        replace_links=lambda *a, **k: None,
        store_snapshot=lambda *a, **k: None,
        upsert=lambda dev, received_at=None: captured["upserts"].append(dev),
        get_prev_snapshot=lambda: None,
        store_change=lambda *a, **k: None,
        set_status=lambda *a, **k: None,
    )
    assert captured["upserts"] == []  # probed, but never upserted without evidence


def test_cycle_stale_lifecycle_uses_cfg_missing_after_sec_not_topology_interval():
    # D2c: the ghost-lifecycle threshold is cfg.missing_after_sec, not the old
    # 3x-topology-interval guess (which at the default 3600s interval would be
    # 10800s -- ten times looser than the 100s configured here).
    devices = [
        {
            "device_nid": "h1",
            "ip": "10.0.0.9",
            "dev_type": "endpoint",
            "status": "up",
            "last_seen": "2026-08-23T00:00:00+00:00",
        }
    ]
    statuses: dict = {}
    reconcile.run_topology_cycle(
        NetdiscoConfig(enabled=True, missing_after_sec=100, topology_interval_sec=3600),
        get_known=lambda: devices,
        get_interfaces=lambda: [],
        get_iface_macs=lambda: {},
        session_factory=lambda ip, cfg: _NoOidSession(),
        collect=lambda *a, **k: [],
        collect_wireless_fn=lambda *a, **k: [],
        collect_med_fn=lambda *a, **k: {},
        replace_links=lambda *a, **k: None,
        store_snapshot=lambda *a, **k: None,
        upsert=lambda *a, **k: None,
        get_prev_snapshot=lambda: None,
        store_change=lambda *a, **k: None,
        set_status=lambda nid, st: statuses.__setitem__(nid, st),
        now="2026-08-23T00:03:20+00:00",  # 200s later: past missing_after_sec=100
    )
    assert statuses == {"h1": "missing"}


def test_cycle_skips_non_rfc1918_infra():
    public_sw = {"device_nid": "nd-chassis-pub", "ip": "8.8.8.8", "dev_type": "router", "mac": ""}
    res = reconcile.run_topology_cycle(
        NetdiscoConfig(enabled=True),
        get_known=lambda: [public_sw],
        get_interfaces=lambda: [],
        get_iface_macs=lambda: {},
        session_factory=lambda ip, cfg: object(),
        collect=_fake_collect,
        replace_links=lambda *a, **k: None,
        store_snapshot=lambda *a, **k: None,
        upsert=lambda *a, **k: None,
        get_prev_snapshot=lambda: None,
        store_change=lambda *a, **k: None,
        set_status=lambda *a, **k: None,
    )
    assert res["probed"] == 0 and res["links"] == 0


def test_topology_cycle_reads_interfaces_once():
    # o5-B5: the cycle used to call get_device() (a per-device query) inside the
    # loop for every probed infra device -- classic N+1. It must instead read all
    # interface-MACs once via get_iface_macs() and never touch get_device() at all.
    devices = [_switch(nid=f"nd-chassis-sw{i}", ip=f"10.0.0.{i}") for i in range(1, 11)]
    iface_calls: list[int] = []

    def counting_iface_macs():
        iface_calls.append(1)
        return {}

    def boom_get_device(nid):
        raise AssertionError("get_device called inside the topology cycle loop (N+1)")

    res = reconcile.run_topology_cycle(
        NetdiscoConfig(enabled=True),
        get_known=lambda: devices,
        get_device=boom_get_device,
        get_interfaces=lambda: [],
        get_iface_macs=counting_iface_macs,
        session_factory=lambda ip, cfg: _NoOidSession(),
        collect=_fake_collect,
        collect_med_fn=lambda *a, **k: {},
        replace_links=lambda *a, **k: None,
        store_snapshot=lambda *a, **k: None,
        upsert=lambda *a, **k: None,
        get_prev_snapshot=lambda: None,
        store_change=lambda *a, **k: None,
        set_status=lambda *a, **k: None,
    )
    assert res["probed"] == 10 and res["busy"] == 0
    assert len(iface_calls) == 1


def test_cycle_idempotent_rerun_does_not_duplicate_links(tmp_path: Path):
    db.init_db(tmp_path / "srp.db")
    db.upsert_net_device(_switch())
    kwargs = {
        "get_known": db.get_net_devices,
        "session_factory": lambda ip, cfg: _NoOidSession(),
        "collect": _fake_collect,
        "collect_med_fn": lambda *a, **k: {},
    }
    reconcile.run_topology_cycle(NetdiscoConfig(enabled=True), **kwargs)
    assert len(db.get_net_links()) == 1
    reconcile.run_topology_cycle(NetdiscoConfig(enabled=True), **kwargs)
    assert len(db.get_net_links()) == 1  # rerun replaces, never duplicates


def test_topology_cycle_does_not_revive_status_when_snmp_times_out(tmp_path: Path):
    """stoperrors P1-4: a device marked "down" by the reachability cycle must stay
    "down" through a topology cycle where every collector comes back empty (SNMP
    timeout) -- the topology cycle must assert "up" only when it actually got
    evidence back, never unconditionally (COALESCE in db.upsert_net_device keeps
    the existing status when the field is omitted from the upsert dict)."""
    db.init_db(tmp_path / "srp.db")
    db.upsert_net_device(_switch())
    db.set_net_device_status("nd-chassis-sw1", "down")
    assert db.get_net_device("nd-chassis-sw1")["status"] == "down"

    reconcile.run_topology_cycle(
        NetdiscoConfig(enabled=True),
        get_known=db.get_net_devices,
        session_factory=lambda ip, cfg: _NoOidSession(),
        collect=lambda *a, **k: [],  # SNMP timeout: no LLDP/CDP/FDB evidence
        collect_wireless_fn=lambda *a, **k: [],
        collect_med_fn=lambda *a, **k: {},
    )
    assert db.get_net_device("nd-chassis-sw1")["status"] == "down"


def test_topology_cycle_med_subtype_does_not_demote_a_richer_stored_subtype(tmp_path: Path):
    """stoperrors P2-4: a neighbour's subtype already set by a higher-priority
    source (e.g. a passive banner confirming "printer") must not be overwritten
    by a lower-priority LLDP-MED classification claiming something else. The
    subtype write used to go through upsert_net_device, whose COALESCE is
    new-wins whenever the new value is non-null -- fixed to route through
    fill_net_device_identity (fill-empty-only, stored subtype wins)."""
    db.init_db(tmp_path / "srp.db")
    switch = _switch()
    db.upsert_net_device(switch)
    neighbor_mac = "00:11:22:33:44:55"
    neighbor_nid = "nd-mac-" + normalize_mac(neighbor_mac)
    db.upsert_net_device({"device_nid": neighbor_nid, "mac": neighbor_mac, "dev_type": "endpoint"})
    db.fill_net_device_identity(neighbor_nid, subtype="printer")  # already set by a richer source
    assert db.get_net_device(neighbor_nid)["subtype"] == "printer"

    lldp_ev = LinkEvidence(switch["device_nid"], normalize_mac(neighbor_mac), SOURCE_LLDP, HIGH, 3)
    reconcile.run_topology_cycle(
        NetdiscoConfig(enabled=True),
        get_known=db.get_net_devices,
        session_factory=lambda ip, cfg: _NoOidSession(),
        collect=lambda netdev, *a, **k: [lldp_ev] if netdev.nid == switch["device_nid"] else [],
        collect_wireless_fn=lambda *a, **k: [],
        collect_med_fn=lambda local, session: {3: "phone"},  # lower-priority classification
    )

    assert db.get_net_device(neighbor_nid)["subtype"] == "printer"  # not demoted to "phone"


# --- reachability correlation cycle (§1.5/§3.7) ---

_REACH_DEVICES = [
    {"device_nid": "R", "ip": "10.0.0.1", "dev_type": "router", "status": "up"},
    {"device_nid": "gw", "ip": "10.0.0.2", "dev_type": "switch", "status": "up"},
    {"device_nid": "h1", "ip": "10.0.0.3", "dev_type": "endpoint", "status": "up"},
]
_REACH_LINKS = [{"a_nid": "R", "b_nid": "gw"}, {"a_nid": "gw", "b_nid": "h1"}]


def test_reachability_noop_when_disabled():
    res = reconcile.run_reachability_cycle(NetdiscoConfig(enabled=False), get_known=lambda: [])
    assert res == {"down": 0, "unreachable": 0, "busy": 0}


def test_reachability_one_root_cause_and_suppressed_downstream():
    statuses: dict = {}
    log: list = []
    res = reconcile.run_reachability_cycle(
        NetdiscoConfig(enabled=True),
        get_known=lambda: _REACH_DEVICES,
        get_links=lambda: _REACH_LINKS,
        is_alive=lambda ip, **k: ip == "10.0.0.1",  # only the router answers
        set_status=lambda nid, st: statuses.__setitem__(nid, st),
        store_change=lambda kind, nid, detail=None, ts=None: log.append((kind, nid)),
    )
    assert statuses["gw"] == "down" and statuses["h1"] == "unreachable"
    assert ("root_cause", "gw") in log  # one cause raised, not a storm
    assert res == {"down": 1, "unreachable": 1, "busy": 0}


def test_reachability_marks_recovered_device_up():
    statuses: dict = {}
    reconcile.run_reachability_cycle(
        NetdiscoConfig(enabled=True),
        get_known=lambda: [
            {"device_nid": "R", "ip": "10.0.0.1", "dev_type": "router", "status": "down"}
        ],
        get_links=lambda: [],
        is_alive=lambda ip, **k: True,  # back online
        set_status=lambda nid, st: statuses.__setitem__(nid, st),
        store_change=lambda *a, **k: None,
    )
    assert statuses["R"] == "up"


def test_reachability_probe_exception_leaves_status_untouched():
    """F1: a probe crash (fd/port exhaustion under the thread fan-out) is NOT the
    same fact as "host unreachable" -- it must never write "down" to the DB. R's
    probe crashes while h1 (linked to R) genuinely fails to answer: R's status must
    stay untouched, and h1 -- a real failure -- must surface as its own root cause
    "down", not get mis-suppressed as "unreachable behind R" the way the old bare
    ``except Exception: return False`` would falsely cascade it (R landing in
    down_set blocks R out of the up-reachable set, stranding every neighbour)."""
    statuses: dict = {}
    changes_log: list = []
    readings: list = []

    def flaky_is_alive(ip, **k):
        if ip == "10.0.0.1":
            raise OSError("fd exhaustion")
        return False  # h1 genuinely does not answer

    devices = [
        {"device_nid": "R", "ip": "10.0.0.1", "dev_type": "router", "status": "up"},
        {"device_nid": "h1", "ip": "10.0.0.2", "dev_type": "endpoint", "status": "up"},
    ]
    res = reconcile.run_reachability_cycle(
        NetdiscoConfig(enabled=True),
        get_known=lambda: devices,
        get_links=lambda: [{"a_nid": "R", "b_nid": "h1"}],
        is_alive=flaky_is_alive,
        set_status=lambda nid, st: statuses.__setitem__(nid, st),
        store_change=lambda kind, nid, detail=None, ts=None: changes_log.append((kind, nid)),
        store_reading=lambda nid, detail, status=None, received_at=None: readings.append(nid),
    )
    assert "R" not in statuses  # probe crashed -- never set to "down"
    assert not any(nid == "R" for _kind, nid in changes_log)
    assert "R" not in readings
    assert statuses.get("h1") == "down"  # real failure surfaces as its own root cause
    assert res["busy"] == 0  # one bad host must never abort the cycle


# --- o5-A12: every status write must also append a reading, or the 24h reachability
# history (S5 sparkline/flap count) stays empty forever -- set_status alone updates
# only the CURRENT status column, never net_device_readings. ---
def test_reachability_cycle_appends_status_readings():
    readings: list = []
    devices = [
        {"device_nid": "R", "ip": "10.0.0.1", "dev_type": "router", "status": "down"},
        {"device_nid": "h1", "ip": "10.0.0.2", "dev_type": "endpoint", "status": "up"},
    ]
    links = [{"a_nid": "R", "b_nid": "h1"}]
    reconcile.run_reachability_cycle(
        NetdiscoConfig(enabled=True),
        get_known=lambda: devices,
        get_links=lambda: links,
        is_alive=lambda ip, **k: ip == "10.0.0.1",  # R answers, h1 doesn't
        set_status=lambda *a, **k: None,
        store_change=lambda *a, **k: None,
        store_reading=lambda nid, detail, status=None, received_at=None: readings.append(
            (nid, status)
        ),
    )
    # h1 goes down (verdicts loop); R recovers up->up (recovery loop) -- both writes
    # must append a reading next to the status write, not just mutate the current column.
    assert len(readings) == 2
    assert dict(readings) == {"h1": "down", "R": "up"}


# --- D2: a host that answers a reachability probe WAS seen right now ---
def test_reachability_cycle_touches_last_seen_for_every_live_device():
    touched: dict = {}
    devices = [
        {"device_nid": "R", "ip": "10.0.0.1", "dev_type": "router", "status": "up"},
        {"device_nid": "h1", "ip": "10.0.0.2", "dev_type": "endpoint", "status": "up"},
    ]
    reconcile.run_reachability_cycle(
        NetdiscoConfig(enabled=True),
        get_known=lambda: devices,
        get_links=lambda: [],
        is_alive=lambda ip, **k: ip == "10.0.0.1",  # only R answers
        set_status=lambda *a, **k: None,
        store_change=lambda *a, **k: None,
        store_reading=lambda *a, **k: None,
        touch_seen=lambda nid, ts=None: touched.__setitem__(nid, ts),
        now="2026-08-23T00:00:00+00:00",
    )
    assert touched == {"R": "2026-08-23T00:00:00+00:00"}  # h1 never answered -- untouched


def test_reachability_cycle_never_touches_seen_for_a_dead_probe():
    touched: dict = {}
    devices = [{"device_nid": "h1", "ip": "10.0.0.2", "dev_type": "endpoint", "status": "up"}]
    reconcile.run_reachability_cycle(
        NetdiscoConfig(enabled=True),
        get_known=lambda: devices,
        get_links=lambda: [],
        is_alive=lambda ip, **k: False,  # never answers
        set_status=lambda *a, **k: None,
        store_change=lambda *a, **k: None,
        store_reading=lambda *a, **k: None,
        touch_seen=lambda nid, ts=None: touched.__setitem__(nid, ts),
    )
    assert touched == {}
