"""Vendor driver factory (Phase 5).

A vendor driver = the generic standard Printer-MIB read, plus a best-effort
overlay of vendor-specific page counters (color / mono / total) fetched from
``oids.VENDOR[vendor]``. Design rules (project invariant -- UNKNOWN over false
data):
  * the overlay is SUPPLEMENTARY: it fills color_pages / mono_pages, and fills
    total_pages only if the standard prtMarkerLifeCount was absent (the standard
    lifetime counter stays authoritative);
  * an absent vendor OID -> None, never a fabricated number;
  * ``oids.VENDOR`` maps are intentionally empty until verified against real
    hardware, so today a vendor driver behaves like the generic one but with the
    vendor label -- generic SNMP already returns totals/supplies/errors for every
    vendor. Fill a vendor map (color/mono/total -> OID) once confirmed on a model.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Dict, Optional

from server.printers import oids
from server.printers.drivers.standard import Session, _i
from server.printers.drivers.standard import read as standard_read
from server.printers.models import PrinterReading


def make_vendor_reader(
    vendor_key: str, vmap: Optional[Dict[str, str]] = None
) -> Callable[..., PrinterReading]:
    """Build a driver that overlays vendor counters onto the generic read.

    ``vmap`` (metric -> OID) defaults to ``oids.VENDOR[vendor_key]``; an explicit
    map is accepted so tests can exercise the overlay without shipping unverified
    OIDs.
    """
    oid_map = vmap if vmap is not None else oids.VENDOR.get(vendor_key, {})

    def read(session: Session, *, ip: str = "") -> PrinterReading:
        reading = replace(standard_read(session, ip=ip))
        reading = replace(reading, vendor=reading.vendor or vendor_key)
        if not oid_map:
            return reading
        got = session.get(list(oid_map.values()))
        vals = {metric: _i(got.get(oid)) for metric, oid in oid_map.items()}
        return replace(
            reading,
            color_pages=vals["color"] if vals.get("color") is not None else reading.color_pages,
            mono_pages=vals["mono"] if vals.get("mono") is not None else reading.mono_pages,
            total_pages=reading.total_pages
            if reading.total_pages is not None
            else vals.get("total"),
        )

    return read
