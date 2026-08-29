"""Оркестрация пробы одного IP → PrinterReading (Phase 5 cascade).

Каскад источников: SNMP (generic Printer-MIB / vendor overlay) → IPP → HTTP.
SNMP подтвердил принтер → читаем драйвером. Иначе пробуем IPP (порт 631), затем
HTTP (порт 80) — оба RFC1918-only и с таймаутом. Первый опознавший источник
побеждает; `source_protocol` проставляет каждый источник. Не принтер / нет
ответа ни от одного источника → None (UNKNOWN, не выдумываем).
"""

from typing import Callable, Optional

from server.printers import classify, http_probe, ipp, snmp
from server.printers.drivers import get_driver
from server.printers.drivers.standard import Session
from server.printers.models import PrinterReading
from server.printers.oids import STANDARD, TABLES

# Дешёвый классификационный набор: серийник (ветка Printer-MIB .43) + sysObjectID
# + sys_descr + один инстанс hrDeviceType (из oids, не дублируем OID).
_HR_DEVICE_TYPE_1 = TABLES["hr_device_type"] + ".1"
_CLASSIFY_OIDS = [
    STANDARD["prt_serial"],
    STANDARD["sys_object_id"],
    STANDARD["sys_descr"],
    _HR_DEVICE_TYPE_1,
]

FallbackFn = Callable[..., Optional[PrinterReading]]


def probe(
    ip: str,
    *,
    community: str = "public",
    version: int = 1,
    port: int = 161,
    timeout: float = 1.0,
    retries: int = 1,
    session: Optional[Session] = None,
    ipp_fn: FallbackFn = ipp.probe,
    http_fn: FallbackFn = http_probe.probe,
) -> Optional[PrinterReading]:
    """Опросить IP каскадом SNMP→IPP→HTTP. Принтер → PrinterReading; иначе None."""
    sess: Session = session or snmp.SnmpSession(
        ip,
        community=community,
        version=version,
        port=port,
        timeout=timeout,
        retries=retries,
    )
    probe_result = sess.get(_CLASSIFY_OIDS)
    if classify.is_printer(probe_result):
        sys_object_id = probe_result.get(STANDARD["sys_object_id"])
        sid = sys_object_id if isinstance(sys_object_id, str) else None
        return get_driver(sid)(sess, ip=ip)
    # SNMP не подтвердил принтер → fallback IPP, затем HTTP (RFC1918+таймаут внутри).
    return ipp_fn(ip, timeout=timeout) or http_fn(ip, timeout=timeout)
