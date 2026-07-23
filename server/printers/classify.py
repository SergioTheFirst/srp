"""Классификатор «это принтер, а не ПК/сервер?».

Срабатывает ТОЛЬКО на определительных сигналах: ответ ветки Printer-MIB
(1.3.6.1.2.1.43), hrDeviceType = hrDevicePrinter или IPP-признак. Vendor-
enterprise sysObjectID сам по себе НЕ считается (HP/Canon делают и не-принтеры) —
UNKNOWN важнее ложной классификации. Значение-исключение (None) не считается
ответом.
"""

from typing import Dict

_PRINTER_MIB = "1.3.6.1.2.1.43"
_HR_DEVICE_TYPE = "1.3.6.1.2.1.25.3.2.1.2"  # hrDeviceType (колонка таблицы)
_HR_DEVICE_PRINTER = "1.3.6.1.2.1.25.3.1.5"  # hrDevicePrinter (значение типа)


def is_printer(probe: Dict[str, object], *, ipp: bool = False) -> bool:
    """True, если зонд показывает принтер. probe = {oid: значение} из SNMP-пробы."""
    if ipp:
        return True
    for oid, value in probe.items():
        if value is None:
            continue
        if oid == _PRINTER_MIB or oid.startswith(_PRINTER_MIB + "."):
            return True
        if (
            oid == _HR_DEVICE_TYPE or oid.startswith(_HR_DEVICE_TYPE + ".")
        ) and value == _HR_DEVICE_PRINTER:
            return True
    return False
