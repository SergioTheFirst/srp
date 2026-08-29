"""Ф7: the topology cycle collects real wireless client<->AP edges off a WLC and
persists them with ``medium=wireless`` (alongside the wired L2 evidence). RED first.
"""

from __future__ import annotations

from server.analytics.oui import normalize_mac
from server.netdisco import oids
from server.netdisco.config import NetdiscoConfig
from server.netdisco.evidence import HIGH, SOURCE_LLDP, LinkEvidence
from server.netdisco.reconcile import run_topology_cycle


def _octet_str(*octs: int) -> str:
    return bytes(octs).decode("latin-1")


class _WlcSession:
    """A fake WLC: answers the AIRESPACE association columns, nothing else."""

    def walk(self, base_oid, *, max_rows=512):
        if base_oid == oids.BSN_MOBILE_STATION_MAC:
            return {f"{base_oid}.1": _octet_str(0x00, 0x11, 0x22, 0x33, 0x44, 0x55)}
        if base_oid == oids.BSN_MOBILE_STATION_AP_MAC:
            return {f"{base_oid}.1": _octet_str(0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF)}
        return {}


class _GetOnlySession:
    """A6: a fake session that only answers the classify-style sysObjectID GET --
    enough to exercise the "stored sys_object_id is NULL" fallback without needing
    the full AIRESPACE walk tables. Defaults to an unclassified (None) answer, so
    it doubles as a harmless stand-in wherever a test's ``collect_wireless_fn`` is
    overridden and the GET's answer does not matter."""

    def __init__(self, sys_object_id=None):
        self._sys_object_id = sys_object_id

    def get(self, oid_list):
        if oid_list == [oids.SYS_OBJECT_ID]:
            return {oids.SYS_OBJECT_ID: self._sys_object_id}
        return {}

    def walk(self, base_oid, *, max_rows=512):
        return {}


def test_topology_cycle_persists_wireless_edges():
    wlc = {
        "device_nid": "nd-mac-wlc",
        "dev_type": "router",  # an on-prem WLC forwards/bridges -> in the probe set
        "ip": "10.0.0.1",
        "mac": None,
        "sys_object_id": "1.3.6.1.4.1.14179.1.1.4.3",  # AIRESPACE root
        "last_seen": "2026-06-27T00:00:00+00:00",
    }
    captured: dict = {}

    def fake_replace(rows, nids, received_at=None):
        captured["rows"] = rows

    run_topology_cycle(
        NetdiscoConfig(enabled=True),
        get_known=lambda: [wlc],
        get_device=lambda nid: wlc,
        session_factory=lambda ip, cfg: _WlcSession(),
        collect=lambda *a, **k: [],  # no wired evidence in this test
        replace_links=fake_replace,
        store_snapshot=lambda *a, **k: None,
        upsert=lambda *a, **k: None,
        get_prev_snapshot=lambda: None,
        store_change=lambda *a, **k: None,
        set_status=lambda *a, **k: None,
        now="2026-06-27T01:00:00+00:00",
    )
    rows = captured.get("rows") or []
    wireless_rows = [r for r in rows if r.get("medium") == "wireless"]
    assert len(wireless_rows) == 1
    assert wireless_rows[0]["via_source"] == "wireless"


def test_topology_cycle_sets_lldp_med_subtype_on_known_neighbor():
    # A switch advertises (via LLDP-MED) a phone on the port whose LLDP neighbour is
    # a KNOWN device -> that neighbour gets subtype "phone" (no phantom row created).
    phone_mac = "00:11:22:33:44:55"
    phone_nid = "nd-mac-" + normalize_mac(phone_mac)
    switch = {
        "device_nid": "nd-mac-sw",
        "dev_type": "switch",
        "ip": "10.0.0.2",
        "mac": None,
        "sys_object_id": None,
        "last_seen": "2026-06-27T00:00:00+00:00",
    }
    phone = {
        "device_nid": phone_nid,
        "dev_type": "endpoint",
        "ip": "10.0.0.9",
        "mac": phone_mac,
        "last_seen": "2026-06-27T00:00:00+00:00",
    }
    devices = [switch, phone]
    fills: list = []

    lldp_ev = LinkEvidence(
        a="nd-mac-sw", b=normalize_mac(phone_mac), source=SOURCE_LLDP, confidence=HIGH, local_if=3
    )

    run_topology_cycle(
        NetdiscoConfig(enabled=True),
        get_known=lambda: devices,
        get_device=lambda nid: next(d for d in devices if d["device_nid"] == nid),
        session_factory=lambda ip, cfg: _GetOnlySession(),
        collect=lambda netdev, *a, **k: [lldp_ev] if netdev.nid == "nd-mac-sw" else [],
        collect_wireless_fn=lambda *a, **k: [],
        collect_med_fn=lambda local, session: {3: "phone"},
        replace_links=lambda *a, **k: None,
        store_snapshot=lambda *a, **k: None,
        upsert=lambda d, now=None: None,
        fill_identity=lambda device_nid, **kw: fills.append({"device_nid": device_nid, **kw}),
        get_prev_snapshot=lambda: None,
        store_change=lambda *a, **k: None,
        set_status=lambda *a, **k: None,
        now="2026-06-27T01:00:00+00:00",
    )
    subtype_fills = [
        f for f in fills if f.get("subtype") == "phone" and f["device_nid"] == phone_nid
    ]
    assert len(subtype_fills) == 1


def test_topology_cycle_skips_med_subtype_for_unknown_neighbor():
    # The same MED advert but the neighbour is NOT a known device -> no upsert (no
    # phantom MAC-less node is fabricated from a neighbour advertisement).
    switch = {
        "device_nid": "nd-mac-sw",
        "dev_type": "switch",
        "ip": "10.0.0.2",
        "mac": None,
        "sys_object_id": None,
        "last_seen": "2026-06-27T00:00:00+00:00",
    }
    fills: list = []
    lldp_ev = LinkEvidence(
        a="nd-mac-sw",
        b=normalize_mac("00:11:22:33:44:55"),
        source=SOURCE_LLDP,
        confidence=HIGH,
        local_if=3,
    )
    run_topology_cycle(
        NetdiscoConfig(enabled=True),
        get_known=lambda: [switch],
        get_device=lambda nid: switch,
        session_factory=lambda ip, cfg: _GetOnlySession(),
        collect=lambda netdev, *a, **k: [lldp_ev],
        collect_wireless_fn=lambda *a, **k: [],
        collect_med_fn=lambda local, session: {3: "phone"},
        replace_links=lambda *a, **k: None,
        store_snapshot=lambda *a, **k: None,
        upsert=lambda d, now=None: None,
        fill_identity=lambda device_nid, **kw: fills.append({"device_nid": device_nid, **kw}),
        get_prev_snapshot=lambda: None,
        store_change=lambda *a, **k: None,
        set_status=lambda *a, **k: None,
        now="2026-06-27T01:00:00+00:00",
    )
    assert not fills  # no phantom fill for a neighbour that isn't a known device


def test_wireless_walk_runs_when_stored_sysobjectid_is_null():
    """A6: sys_object_id is written only by the classifier -- a device that has not
    been classified yet has a NULL column, which used to close the
    wireless.collect_wireless vendor gate forever (a WLC would never be walked
    until something else happened to classify it first). reconcile must GET the
    live sysObjectID once when the stored column is empty, so the wireless walk
    still runs on a not-yet-classified WLC."""
    wlc = {
        "device_nid": "nd-mac-wlc",
        "dev_type": "router",
        "ip": "10.0.0.1",
        "mac": None,
        "sys_object_id": None,  # not yet classified
        "last_seen": "2026-06-27T00:00:00+00:00",
    }
    captured: dict = {}

    def fake_collect_wireless(session, *, sys_object_id):
        captured["sys_object_id"] = sys_object_id
        return []

    run_topology_cycle(
        NetdiscoConfig(enabled=True),
        get_known=lambda: [wlc],
        get_device=lambda nid: wlc,
        session_factory=lambda ip, cfg: _GetOnlySession("1.3.6.1.4.1.14179.1.1.4.3"),
        collect=lambda *a, **k: [],
        collect_wireless_fn=fake_collect_wireless,
        replace_links=lambda *a, **k: None,
        store_snapshot=lambda *a, **k: None,
        upsert=lambda *a, **k: None,
        get_prev_snapshot=lambda: None,
        store_change=lambda *a, **k: None,
        set_status=lambda *a, **k: None,
        now="2026-06-27T01:00:00+00:00",
    )
    assert captured.get("sys_object_id") == "1.3.6.1.4.1.14179.1.1.4.3"
