"""Оркестрация пробы одного IP → PrinterReading.

SNMP-проба → классификация (принтер?) → выбор драйвера по sysObjectID → read.
Не принтер / недоступен → None (UNKNOWN, не выдумываем). IPP/HTTP-fallback
подключаются в Phase 5. Источник пока всегда "snmp".
"""

from typing import Optional

from server.printers import classify, snmp
from server.printers.drivers import get_driver
from server.printers.drivers.standard import Session
from server.printers.models import PrinterReading
from server.printers.oids import STANDARD

# Дешёвый классификационный набор: серийник (ветка Printer-MIB .43) + sysObjectID
# + sys_descr + один инстанс hrDeviceType.
_HR_DEVICE_TYPE_1 = "1.3.6.1.2.1.25.3.2.1.2.1"
_CLASSIFY_OIDS = [
    STANDARD["prt_serial"],
    STANDARD["sys_object_id"],
    STANDARD["sys_descr"],
    _HR_DEVICE_TYPE_1,
]


def probe(
    ip: str,
    *,
    community: str = "public",
    version: int = 1,
    port: int = 161,
    timeout: float = 1.0,
    retries: int = 1,
    session: Optional[Session] = None,
) -> Optional[PrinterReading]:
    """Опросить IP. Принтер → PrinterReading; не принтер / нет ответа → None."""
    sess: Session = session or snmp.SnmpSession(
        ip,
        community=community,
        version=version,
        port=port,
        timeout=timeout,
        retries=retries,
    )
    probe_result = sess.get(_CLASSIFY_OIDS)
    if not classify.is_printer(probe_result):
        return None
    sys_object_id = probe_result.get(STANDARD["sys_object_id"])
    sid = sys_object_id if isinstance(sys_object_id, str) else None
    driver = get_driver(sid)
    return driver(sess, ip=ip)
