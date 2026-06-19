"""Phase 5 — vendor driver registry + supplementary overlay mechanism.

The overlay is tested with synthetic OID maps (not real vendor OIDs): it must fill
color/mono from vendor OIDs, keep the standard total authoritative, and return
None for an absent vendor OID (UNKNOWN over a fabricated number).
"""

from __future__ import annotations

import pytest
from server.printers.drivers import get_driver, standard
from server.printers.drivers.vendor import make_vendor_reader
from server.printers.oids import STANDARD

pytestmark = pytest.mark.unit


class FakeSession:
    def __init__(self, scalars=None, tables=None):
        self._scalars = scalars or {}
        self._tables = tables or {}

    def get(self, oid_list):
        return {o: self._scalars[o] for o in oid_list if o in self._scalars}

    def walk(self, base_oid):
        return self._tables.get(base_oid, {})


def test_registry_picks_vendor_driver_for_known_sysobjectid():
    hp_driver = get_driver("1.3.6.1.4.1.11.2.3.9.1")  # enterprise 11 = hp
    assert hp_driver is not standard.read


def test_registry_falls_back_to_standard_for_unknown():
    assert get_driver("1.3.6.1.4.1.99999.1") is standard.read
    assert get_driver(None) is standard.read


def test_vendor_overlay_fills_color_mono_from_vendor_oids():
    read = make_vendor_reader(
        "hp", vmap={"color": "1.3.6.1.4.1.11.color", "mono": "1.3.6.1.4.1.11.mono"}
    )
    sess = FakeSession(
        {
            STANDARD["prt_serial"]: "CN1",
            STANDARD["prt_marker_life_count"]: 5000,
            "1.3.6.1.4.1.11.color": 1200,
            "1.3.6.1.4.1.11.mono": 3800,
        }
    )
    r = read(sess, ip="192.168.1.5")
    assert r.total_pages == 5000  # standard stays authoritative
    assert r.color_pages == 1200 and r.mono_pages == 3800
    assert r.vendor == "hp"


def test_vendor_overlay_absent_oid_stays_none_no_fabrication():
    read = make_vendor_reader("hp", vmap={"color": "1.3.6.1.4.1.11.color"})
    sess = FakeSession({STANDARD["prt_serial"]: "CN1"})  # vendor OID absent
    assert read(sess, ip="192.168.1.5").color_pages is None


def test_vendor_overlay_total_only_when_standard_absent():
    read = make_vendor_reader("kyocera", vmap={"total": "1.3.6.1.4.1.1347.total"})
    sess = FakeSession({STANDARD["prt_serial"]: "K1", "1.3.6.1.4.1.1347.total": 9000})
    assert read(sess, ip="192.168.1.6").total_pages == 9000  # standard absent -> vendor fills
    sess2 = FakeSession(
        {
            STANDARD["prt_serial"]: "K1",
            STANDARD["prt_marker_life_count"]: 100,
            "1.3.6.1.4.1.1347.total": 9000,
        }
    )
    assert read(sess2, ip="192.168.1.6").total_pages == 100  # standard wins


def test_empty_vendor_map_is_standard_plus_label():
    read = make_vendor_reader("epson", vmap={})
    sess = FakeSession({STANDARD["prt_serial"]: "E1", STANDARD["prt_marker_life_count"]: 42})
    r = read(sess, ip="192.168.1.7")
    assert r.total_pages == 42 and r.serial == "E1" and r.vendor == "epson"
