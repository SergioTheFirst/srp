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


def _fake_collect(device, session, *, infra_macs=frozenset()):
    return [LinkEvidence(device.nid, HOST_AA, SOURCE_FDB_EDGE, HIGH, 3)]


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
        session_factory=lambda ip, cfg: object(),
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


def test_cycle_skips_non_rfc1918_infra():
    public_sw = {"device_nid": "nd-chassis-pub", "ip": "8.8.8.8", "dev_type": "router", "mac": ""}
    res = reconcile.run_topology_cycle(
        NetdiscoConfig(enabled=True),
        get_known=lambda: [public_sw],
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


def test_cycle_idempotent_rerun_does_not_duplicate_links(tmp_path: Path):
    db.init_db(tmp_path / "srp.db")
    db.upsert_net_device(_switch())
    kwargs = {
        "get_known": db.get_net_devices,
        "session_factory": lambda ip, cfg: object(),
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
        session_factory=lambda ip, cfg: object(),
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
        session_factory=lambda ip, cfg: object(),
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
