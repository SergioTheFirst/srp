"""Единая база OID принтеров — единственный источник истины.

Стандарт: Printer-MIB (RFC 3805), HOST-RESOURCES-MIB (RFC 2790),
MIB-II / SNMPv2-MIB (RFC 1213 / RFC 3418). Vendor-резолвинг по enterprise-PEN
(IANA). Дубли значений внутри группы запрещены (см. tests/test_printer_oids.py).

Соглашение:
- STANDARD — скалярные OID с фиксированным индексом инстанса (готовы к GET).
- TABLES   — базовые OID табличных колонок БЕЗ индекса (для snmp_walk).
Vendor-специфичные OID (счётчики/расходники, где стандарт пуст) добавляются
в Phase 5 по файлу на вендора; здесь — только стандарт + карта префиксов.
"""

from typing import Dict, Optional

# --- Скаляры (GET): MIB-II system.* + Printer-MIB General -------------------
STANDARD: Dict[str, str] = {
    # SNMPv2-MIB / MIB-II  system  (1.3.6.1.2.1.1)
    "sys_descr": "1.3.6.1.2.1.1.1.0",
    "sys_object_id": "1.3.6.1.2.1.1.2.0",
    "sys_uptime": "1.3.6.1.2.1.1.3.0",
    "sys_contact": "1.3.6.1.2.1.1.4.0",
    "sys_name": "1.3.6.1.2.1.1.5.0",
    "sys_location": "1.3.6.1.2.1.1.6.0",
    # Printer-MIB  prtGeneral  (1.3.6.1.2.1.43.5)
    "prt_config_changes": "1.3.6.1.2.1.43.5.1.1.1.1",  # prtGeneralConfigChanges
    "prt_serial": "1.3.6.1.2.1.43.5.1.1.17.1",  # prtGeneralSerialNumber
    # Printer-MIB  prtMarker  (общий счётчик отпечатков, маркер 1)
    "prt_marker_life_count": "1.3.6.1.2.1.43.10.2.1.4.1.1",  # prtMarkerLifeCount.1.1
}

# --- Таблицы (WALK): расходники, лотки, ошибки, крышки, консоль, интерфейсы --
TABLES: Dict[str, str] = {
    # prtMarkerSupplies  (1.3.6.1.2.1.43.11.1.1) — тонеры/картриджи/барабаны
    # prtMarkerSuppliesClass: supplyThatIsConsumed(3) / receptacleThatIsFilled(4)
    "supply_class": "1.3.6.1.2.1.43.11.1.1.4",
    "supply_type": "1.3.6.1.2.1.43.11.1.1.5",  # toner/ink/drum/...
    "supply_desc": "1.3.6.1.2.1.43.11.1.1.6",
    "supply_unit": "1.3.6.1.2.1.43.11.1.1.7",
    "supply_max": "1.3.6.1.2.1.43.11.1.1.8",  # prtMarkerSuppliesMaxCapacity
    "supply_level": "1.3.6.1.2.1.43.11.1.1.9",  # prtMarkerSuppliesLevel
    # prtInput  (1.3.6.1.2.1.43.8.2.1) — лотки подачи бумаги
    "input_max": "1.3.6.1.2.1.43.8.2.1.9",  # prtInputMaxCapacity
    "input_current": "1.3.6.1.2.1.43.8.2.1.10",  # prtInputCurrentLevel
    "input_status": "1.3.6.1.2.1.43.8.2.1.11",
    "input_media": "1.3.6.1.2.1.43.8.2.1.12",  # prtInputMediaName
    "input_name": "1.3.6.1.2.1.43.8.2.1.13",
    # prtAlert  (1.3.6.1.2.1.43.18.1.1) — таблица активных ошибок/предупреждений
    "prt_alert_severity": "1.3.6.1.2.1.43.18.1.1.2",
    "prt_alert_code": "1.3.6.1.2.1.43.18.1.1.7",
    "prt_alert_desc": "1.3.6.1.2.1.43.18.1.1.8",
    # prtCover  (1.3.6.1.2.1.43.6.1.1) — крышки/дверцы
    "prt_cover_desc": "1.3.6.1.2.1.43.6.1.1.2",
    "prt_cover_status": "1.3.6.1.2.1.43.6.1.1.3",
    # prtConsoleDisplayBuffer  (1.3.6.1.2.1.43.16.5.1) — текст на дисплее
    "prt_console_text": "1.3.6.1.2.1.43.16.5.1.2",
    # HOST-RESOURCES  hrDevice / hrPrinter  (1.3.6.1.2.1.25.3)
    "hr_device_type": "1.3.6.1.2.1.25.3.2.1.2",
    "hr_device_descr": "1.3.6.1.2.1.25.3.2.1.3",
    "hr_device_status": "1.3.6.1.2.1.25.3.2.1.5",
    "hr_printer_status": "1.3.6.1.2.1.25.3.5.1.1",  # hrPrinterStatus
    "hr_detected_error": "1.3.6.1.2.1.25.3.5.1.2",  # hrPrinterDetectedErrorState
    # MIB-II  ifPhysAddress  (1.3.6.1.2.1.2.2.1.6) — MAC интерфейсов
    "if_phys_address": "1.3.6.1.2.1.2.2.1.6",
}

# --- Vendor по enterprise-PEN (IANA): sysObjectID = 1.3.6.1.4.1.<N>.* -------
# UNKNOWN важнее ложной уверенности → только проверенные номера.
_VENDOR_ENTERPRISE: Dict[str, str] = {
    "11": "hp",
    "236": "samsung",
    "253": "xerox",
    "367": "ricoh",
    "641": "lexmark",
    "674": "dell",
    "1248": "epson",
    "1347": "kyocera",
    "1602": "canon",
    "2001": "oki",
    "2435": "brother",
    "18334": "konica_minolta",
}

_ENTERPRISE_PREFIX = "1.3.6.1.4.1."


def vendor_for_sysobjectid(sysobjectid: str) -> Optional[str]:
    """Вернуть код вендора по sysObjectID или None, если префикс неизвестен.

    Резолвинг — по первому enterprise-арку после 1.3.6.1.4.1; неизвестный
    номер → None (не выдумываем вендора).
    """
    if not sysobjectid.startswith(_ENTERPRISE_PREFIX):
        return None
    enterprise = sysobjectid[len(_ENTERPRISE_PREFIX) :].split(".", 1)[0]
    return _VENDOR_ENTERPRISE.get(enterprise)
