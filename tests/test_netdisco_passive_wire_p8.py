"""Ф8 wiring: fill-empty-only identity writer + passive enrichment cycle + T1.

Passive identity is the LOWEST-priority source: a reverse-DNS/mDNS/NetBIOS hint
may only fill an *empty* ``net_devices.hostname``/``subtype``/``model`` -- it must
never overwrite a value an agent or SNMP probe already established. The DB writer
enforces that with ``COALESCE(existing, new)`` (the mirror image of the COALESCE
in ``upsert_net_device``, which lets a fresh probe win). The cycle then maps each
passive hint to a known node by IP, never creates a node, and only ever touches
private inventory under the shared poll lock. RED first.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List

import server.db as db
from server.netdisco import scheduler
from server.netdisco.config import NetdiscoConfig
from server.netdisco.passive import PassiveHint


def _row(path: Path, nid: str) -> dict:
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    try:
        return dict(con.execute("SELECT * FROM net_devices WHERE device_nid=?", (nid,)).fetchone())
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# fill_net_device_identity -- fill-empty-only (COALESCE(existing, new))         #
# --------------------------------------------------------------------------- #


def test_fill_sets_empty_fields(tmp_path: Path) -> None:
    p = tmp_path / "srp.db"
    db.init_db(p)
    db.upsert_net_device({"device_nid": "nd-1", "ip": "10.0.0.9", "dev_type": "unknown"})
    db.fill_net_device_identity("nd-1", hostname="Office-Printer", subtype="printer")
    row = _row(p, "nd-1")
    assert row["hostname"] == "Office-Printer"
    assert row["subtype"] == "printer"


def test_fill_never_overwrites_existing(tmp_path: Path) -> None:
    p = tmp_path / "srp.db"
    db.init_db(p)
    # A prior SNMP/agent pass already set a richer hostname + subtype.
    db.upsert_net_device(
        {"device_nid": "nd-2", "hostname": "switch-core", "subtype": "ap", "dev_type": "switch"}
    )
    db.fill_net_device_identity("nd-2", hostname="ptr-name", subtype="workstation", model="m")
    row = _row(p, "nd-2")
    assert row["hostname"] == "switch-core"  # existing wins
    assert row["subtype"] == "ap"  # existing wins
    assert row["model"] == "m"  # model was empty -> filled


def test_fill_unknown_nid_is_noop(tmp_path: Path) -> None:
    p = tmp_path / "srp.db"
    db.init_db(p)
    db.fill_net_device_identity("nd-missing", hostname="x")  # no row -> no error, no insert
    con = sqlite3.connect(str(p))
    try:
        assert con.execute("SELECT COUNT(*) FROM net_devices").fetchone()[0] == 0
    finally:
        con.close()


def test_fill_all_none_is_noop(tmp_path: Path) -> None:
    p = tmp_path / "srp.db"
    db.init_db(p)
    db.upsert_net_device({"device_nid": "nd-3", "hostname": "keep", "dev_type": "router"})
    db.fill_net_device_identity("nd-3")  # nothing to fill -> no-op
    assert _row(p, "nd-3")["hostname"] == "keep"


# --------------------------------------------------------------------------- #
# run_passive_cycle -- gate, lock, map-by-IP, fill                             #
# --------------------------------------------------------------------------- #


def _cfg(**kw: Any) -> NetdiscoConfig:
    base: Dict[str, Any] = {"enabled": True, "passive_enabled": True}
    base.update(kw)
    return NetdiscoConfig(**base)


def test_passive_cycle_disabled_does_nothing() -> None:
    calls: List[str] = []
    out = scheduler.run_passive_cycle(
        NetdiscoConfig(enabled=True, passive_enabled=False),
        get_known=lambda: calls.append("known") or [],
    )
    assert out == {"enriched": 0, "busy": 0}
    assert calls == []  # gated before any read


def test_passive_cycle_maps_hints_by_ip_and_fills() -> None:
    devices = [
        {"device_nid": "nd-a", "ip": "10.0.0.9", "dev_type": "unknown", "hostname": None},
        {"device_nid": "nd-b", "ip": "10.0.0.3", "dev_type": "endpoint", "hostname": None},
    ]
    filled: Dict[str, dict] = {}

    def fill(nid: str, **kw: Any) -> None:
        filled[nid] = {k: v for k, v in kw.items() if v is not None}

    out = scheduler.run_passive_cycle(
        _cfg(),
        get_known=lambda: devices,
        fill=fill,
        resolve_names_fn=lambda ips, **k: {"10.0.0.9": "office-printer.lan"},
        collect_mdns_fn=lambda **k: {
            "10.0.0.9": PassiveHint("10.0.0.9", "mdns", subtype="printer")
        },
        collect_ssdp_fn=lambda **k: {},
        collect_wsd_fn=lambda **k: {},
        collect_netbios_fn=lambda targets, **k: {
            "10.0.0.3": PassiveHint(
                "10.0.0.3", "netbios", hostname="DESKTOP-7", subtype="workstation"
            )
        },
        collect_banner_fn=lambda targets, **k: {},
        get_printer_ip_map=lambda: [],
    )
    assert out["busy"] == 0
    assert out["enriched"] == 2
    assert filled["nd-a"]["hostname"] == "office-printer.lan"
    assert filled["nd-a"]["subtype"] == "printer"
    assert filled["nd-b"]["hostname"] == "DESKTOP-7"
    assert filled["nd-b"]["subtype"] == "workstation"


def test_passive_cycle_ignores_responders_not_in_inventory() -> None:
    devices = [{"device_nid": "nd-a", "ip": "10.0.0.9", "dev_type": "unknown", "hostname": None}]
    filled: Dict[str, dict] = {}
    out = scheduler.run_passive_cycle(
        _cfg(),
        get_known=lambda: devices,
        fill=lambda nid, **kw: filled.__setitem__(nid, kw),
        resolve_names_fn=lambda ips, **k: {"10.0.0.99": "stranger.lan"},  # not in inventory
        collect_mdns_fn=lambda **k: {},
        collect_ssdp_fn=lambda **k: {},
        collect_wsd_fn=lambda **k: {},
        collect_netbios_fn=lambda targets, **k: {},
        collect_banner_fn=lambda targets, **k: {},
        get_printer_ip_map=lambda: [],
    )
    assert out["enriched"] == 0
    assert filled == {}  # never invents a node from a passive responder


def test_passive_cycle_t1_printer_ip_map_subtype() -> None:
    devices = [{"device_nid": "nd-p", "ip": "10.0.0.50", "dev_type": "unknown", "hostname": None}]
    filled: Dict[str, dict] = {}
    out = scheduler.run_passive_cycle(
        _cfg(),
        get_known=lambda: devices,
        fill=lambda nid, **kw: filled.__setitem__(nid, {k: v for k, v in kw.items() if v}),
        resolve_names_fn=lambda ips, **k: {},
        collect_mdns_fn=lambda **k: {},
        collect_ssdp_fn=lambda **k: {},
        collect_wsd_fn=lambda **k: {},
        collect_netbios_fn=lambda targets, **k: {},
        collect_banner_fn=lambda targets, **k: {},
        get_printer_ip_map=lambda: [{"device_id": "d1", "name": "HP-Office", "ip": "10.0.0.50"}],
    )
    assert out["enriched"] == 1
    assert filled["nd-p"]["subtype"] == "printer"


def test_passive_cycle_targets_only_private_addresses() -> None:
    devices = [
        {"device_nid": "nd-pub", "ip": "8.8.8.8", "dev_type": "unknown", "hostname": None},
        {"device_nid": "nd-priv", "ip": "10.0.0.9", "dev_type": "unknown", "hostname": None},
    ]
    seen_targets: List[List[str]] = []

    def netbios(targets, **k):
        seen_targets.append(list(targets))
        return {}

    scheduler.run_passive_cycle(
        _cfg(),
        get_known=lambda: devices,
        fill=lambda nid, **kw: None,
        resolve_names_fn=lambda ips, **k: {},
        collect_mdns_fn=lambda **k: {},
        collect_ssdp_fn=lambda **k: {},
        collect_wsd_fn=lambda **k: {},
        collect_netbios_fn=netbios,
        collect_banner_fn=lambda targets, **k: {},
        get_printer_ip_map=lambda: [],
    )
    assert seen_targets and "8.8.8.8" not in seen_targets[0]
    assert "10.0.0.9" in seen_targets[0]
