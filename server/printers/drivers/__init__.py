"""Реестр драйверов: выбор по sysObjectID, fallback на generic standard.

Vendor-драйверы (Phase 5) = generic Printer-MIB + supplementary vendor overlay
(см. drivers/vendor.py). Неизвестный вендор / нет vendor-драйвера → standard.
Драйвер = callable read(session, *, ip).
"""

from typing import Callable, Dict, Optional

from server.printers import oids
from server.printers.drivers import (
    brother,
    canon,
    epson,
    hp,
    kyocera,
    lexmark,
    ricoh,
    standard,
    xerox,
)
from server.printers.models import PrinterReading

Driver = Callable[..., PrinterReading]

_VENDOR_DRIVERS: Dict[str, Driver] = {
    "hp": hp.read,
    "xerox": xerox.read,
    "kyocera": kyocera.read,
    "canon": canon.read,
    "brother": brother.read,
    "lexmark": lexmark.read,
    "ricoh": ricoh.read,
    "epson": epson.read,
}


def get_driver(sys_object_id: Optional[str]) -> Driver:
    """Драйвер по sysObjectID. Неизвестный вендор / нет vendor-драйвера → standard."""
    vendor = oids.vendor_for_sysobjectid(sys_object_id or "")
    return _VENDOR_DRIVERS.get(vendor or "", standard.read)
