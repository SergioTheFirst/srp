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
from server.netdisco.evidence import HIGH, SOURCE_FDB_EDGE, LinkEvidence

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
        replace_links=lambda rows, nodes, received_at=None: captured.update(rows=rows, nodes=nodes),
        store_snapshot=lambda graph, received_at=None: captured.update(graph=graph),
        upsert=lambda dev, received_at=None: captured.setdefault("upserts", []).append(dev),
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
    )
    assert res["probed"] == 0 and res["links"] == 0


def test_cycle_idempotent_rerun_does_not_duplicate_links(tmp_path: Path):
    db.init_db(tmp_path / "srp.db")
    db.upsert_net_device(_switch())
    kwargs = dict(
        get_known=db.get_net_devices,
        session_factory=lambda ip, cfg: object(),
        collect=_fake_collect,
    )
    reconcile.run_topology_cycle(NetdiscoConfig(enabled=True), **kwargs)
    assert len(db.get_net_links()) == 1
    reconcile.run_topology_cycle(NetdiscoConfig(enabled=True), **kwargs)
    assert len(db.get_net_links()) == 1  # rerun replaces, never duplicates
