"""Phase 6 -- driver registry (RED first).

A driver supplies vendor-specific enrichment (model/serial from vendor OIDs).
``select_driver(sys_object_id)`` picks by vendor, falling back to ``standard``.
P6 ships no vendor drivers: their OID maps are empty until verified on real
hardware (honesty over invented OIDs), so every device gets the generic driver.
The seam is real -- a registered vendor driver is selected by its vendor key.
"""

from __future__ import annotations

from server.netdisco import drivers
from server.netdisco.drivers import standard


def test_select_driver_defaults_to_standard():
    assert drivers.select_driver(None) is standard.read
    assert drivers.select_driver("1.3.6.1.4.1.9.1.1") is standard.read  # cisco PEN, no driver


def test_standard_vendor_oids_are_empty_until_hardware_verified():
    assert standard.VENDOR_OIDS == {}


def test_standard_read_returns_no_extras():
    assert standard.read(object()) == {}


def test_registered_vendor_driver_is_selected(monkeypatch):
    def fake_driver(session, **kwargs):
        return {"model": "ACME-9000"}

    monkeypatch.setattr(drivers, "vendor_for_sysobjectid", lambda s: "acme")
    monkeypatch.setitem(drivers._VENDOR_DRIVERS, "acme", fake_driver)
    assert drivers.select_driver("1.3.6.1.4.1.99999.1") is fake_driver
