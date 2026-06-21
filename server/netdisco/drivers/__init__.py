"""Netdisco driver registry: pick by sysObjectID vendor, fallback to standard.

No vendor drivers ship in P6 -- their OID maps are empty until hardware-verified,
so ``select_driver`` returns the generic ``standard`` driver for every device. The
seam exists so a verified vendor driver (Cisco / MikroTik / Aruba / ...) registers
in ``_VENDOR_DRIVERS`` later without touching the probe or the classifier. Vendor
resolution reuses the printer PEN->vendor resolver (a generic enterprise-PEN map,
not printer-specific in mechanism).
"""

from __future__ import annotations

from typing import Callable, Dict, Optional

from server.netdisco.drivers import standard
from server.printers.oids import vendor_for_sysobjectid

Driver = Callable[..., Dict[str, Optional[str]]]

# Registered after a vendor's OIDs are verified on real hardware. Empty in P6.
_VENDOR_DRIVERS: Dict[str, Driver] = {}


def select_driver(sys_object_id: Optional[str]) -> Driver:
    """Driver for a device by its sysObjectID vendor; unknown vendor -> standard."""
    vendor = vendor_for_sysobjectid(sys_object_id or "")
    return _VENDOR_DRIVERS.get(vendor or "", standard.read)
