"""Ф7 T6: real wireless client<->AP association edges from a WLC.

Only a device whose ``sysObjectID`` is under a confirmed wireless-controller
enterprise root (AIRESPACE / Aruba / MikroTik) is walked -- a generic host that
happens to answer SNMP is never mistaken for a WLC (fail-closed). The client and
AP MAC columns are joined by their shared table index into one HIGH wireless edge.
RED first.
"""

from __future__ import annotations

from server.analytics.oui import normalize_mac
from server.netdisco import evidence, oids, wireless


class FakeSession:
    def __init__(self, tables=None):
        self._tables = tables or {}

    def walk(self, base_oid, *, max_rows=512):
        return dict(self._tables.get(base_oid, {}))


def _octet_str(*octs: int) -> str:
    return bytes(octs).decode("latin-1")


def test_collect_wireless_pairs_client_to_ap_airespace():
    client_oid = oids.BSN_MOBILE_STATION_MAC
    ap_oid = oids.BSN_MOBILE_STATION_AP_MAC
    idx = "1.2.3.4.5.6"
    session = FakeSession(
        {
            client_oid: {f"{client_oid}.{idx}": _octet_str(0x00, 0x11, 0x22, 0x33, 0x44, 0x55)},
            ap_oid: {f"{ap_oid}.{idx}": _octet_str(0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF)},
        }
    )
    evs = wireless.collect_wireless(session, sys_object_id="1.3.6.1.4.1.14179.1.1.4.3")
    assert len(evs) == 1
    ev = evs[0]
    assert {ev.a, ev.b} == {
        normalize_mac("00:11:22:33:44:55"),
        normalize_mac("aa:bb:cc:dd:ee:ff"),
    }
    assert ev.source == evidence.SOURCE_WIRELESS
    assert ev.medium == "wireless"
    assert ev.confidence == evidence.HIGH


def test_collect_wireless_accepts_text_mac_values():
    # Some controllers hand the MAC back as text, not raw octets.
    client_oid = oids.ARUBA_USER_STA_MAC
    ap_oid = oids.ARUBA_USER_AP_MAC
    session = FakeSession(
        {
            client_oid: {f"{client_oid}.9": "00:11:22:33:44:55"},
            ap_oid: {f"{ap_oid}.9": "AA:BB:CC:DD:EE:FF"},
        }
    )
    evs = wireless.collect_wireless(session, sys_object_id="1.3.6.1.4.1.14823.1.1.1")
    assert len(evs) == 1
    assert {evs[0].a, evs[0].b} == {
        normalize_mac("00:11:22:33:44:55"),
        normalize_mac("aa:bb:cc:dd:ee:ff"),
    }


def test_collect_wireless_skips_non_wlc():
    # A Cisco IOS switch (enterprise 9) is not a WLC root -> never walked.
    session = FakeSession(
        {oids.BSN_MOBILE_STATION_AP_MAC: {f"{oids.BSN_MOBILE_STATION_AP_MAC}.1": "x"}}
    )
    assert wireless.collect_wireless(session, sys_object_id="1.3.6.1.4.1.9.1.516") == []


def test_collect_wireless_none_or_prefix_collision_is_fail_closed():
    assert wireless.collect_wireless(FakeSession({}), sys_object_id=None) == []
    # "141790" must not match the "14179" root (dot-boundary required).
    session = FakeSession(
        {oids.BSN_MOBILE_STATION_AP_MAC: {f"{oids.BSN_MOBILE_STATION_AP_MAC}.1": "x"}}
    )
    assert wireless.collect_wireless(session, sys_object_id="1.3.6.1.4.1.141790.1") == []


def test_collect_wireless_drops_unpaired_rows():
    # A client row with no matching AP index (and vice versa) yields nothing.
    client_oid = oids.BSN_MOBILE_STATION_MAC
    ap_oid = oids.BSN_MOBILE_STATION_AP_MAC
    session = FakeSession(
        {
            client_oid: {f"{client_oid}.1.1": _octet_str(0x00, 0x11, 0x22, 0x33, 0x44, 0x55)},
            ap_oid: {f"{ap_oid}.2.2": _octet_str(0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF)},
        }
    )
    assert wireless.collect_wireless(session, sys_object_id="1.3.6.1.4.1.14179.1") == []
