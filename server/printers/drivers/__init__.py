"""Реестр драйверов: выбор по sysObjectID, fallback на generic standard.

Phase 2: vendor-резолвинг работает, но vendor-драйверы появятся в Phase 5 —
пока любой принтер обслуживает standard. Драйвер = callable read(session, *, ip).
"""

from typing import Callable, Dict, Optional

from server.printers import oids
from server.printers.drivers import standard
from server.printers.models import PrinterReading

Driver = Callable[..., PrinterReading]

# Заполняется в Phase 5: {"hp": hp.read, "xerox": xerox.read, ...}.
_VENDOR_DRIVERS: Dict[str, Driver] = {}


def get_driver(sys_object_id: Optional[str]) -> Driver:
    """Драйвер по sysObjectID. Неизвестный вендор / нет vendor-драйвера → standard."""
    vendor = oids.vendor_for_sysobjectid(sys_object_id or "")
    return _VENDOR_DRIVERS.get(vendor or "", standard.read)
