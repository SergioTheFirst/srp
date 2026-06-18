"""Generic-драйвер: стандартный Printer-MIB / HOST-RESOURCES профиль.

Собирает PrinterReading из ответов SNMP через duck-typed сессию (.get/.walk).
Машинные значения (статус, тип расходника) — латиницей (инвариант проекта).
Неизвестное → None / пустой кортеж; ничего не выдумываем.
"""

from typing import Dict, List, Optional, Protocol, Tuple

from server.printers import oids
from server.printers.models import PrinterError, PrinterReading, Supply, Tray

STANDARD = oids.STANDARD
TABLES = oids.TABLES

# hrPrinterStatus → машинное значение.
_PRINTER_STATUS = {1: "other", 2: "unknown", 3: "idle", 4: "printing", 5: "warmup"}
# prtMarkerSuppliesType → машинное значение (частые типы; прочее → "other").
_SUPPLY_TYPE = {
    1: "other",
    2: "unknown",
    3: "toner",
    4: "wasteToner",
    5: "ink",
    6: "inkCartridge",
    7: "inkRibbon",
    8: "wasteInk",
    9: "opc",
    10: "developer",
    11: "fuserOil",
    15: "fuser",
    18: "cleanerUnit",
    20: "transferUnit",
    21: "tonerCartridge",
}


class Session(Protocol):
    """Что драйверу нужно от SNMP-сессии (см. snmp.SnmpSession)."""

    def get(self, oids: List[str]) -> Dict[str, object]: ...

    def walk(self, base_oid: str) -> Dict[str, object]: ...


def _s(value: object) -> Optional[str]:
    return value if isinstance(value, str) and value != "" else None


def _i(value: object) -> Optional[int]:
    return value if isinstance(value, int) else None


def _supply_type(raw: Optional[int]) -> str:
    if raw is None:
        return "unknown"
    return _SUPPLY_TYPE.get(raw, "other")


# prtMarkerSuppliesClass: supplyThatIsConsumed(3) / receptacleThatIsFilled(4).
# Различает «остаток тонера» (низкий = плохо) и «заполнение отработки» (высокий = плохо).
_SUPPLY_CLASS = {3: "consumed", 4: "receptacle"}


def _supply_class(raw: Optional[int]) -> Optional[str]:
    return _SUPPLY_CLASS.get(raw) if raw is not None else None


def _by_index(walked: Dict[str, object], base: str) -> Dict[str, object]:
    """{полный_OID: значение} → {суффикс-индекс: значение}."""
    prefix = base + "."
    return {oid[len(prefix) :]: val for oid, val in walked.items() if oid.startswith(prefix)}


def _sorted_indices(*tables: Dict[str, object]) -> List[str]:
    keys = set()
    for table in tables:
        keys |= set(table.keys())
    # Только числовые суффиксы-индексы: битый/пустой ключ от кривого агента не
    # должен ронять read() через int() (review HIGH).
    valid = [k for k in keys if k and all(p.isdigit() for p in k.split("."))]
    return sorted(valid, key=lambda s: tuple(int(p) for p in s.split(".")))


def _read_supplies(session: Session) -> Tuple[Supply, ...]:
    desc = _by_index(session.walk(TABLES["supply_desc"]), TABLES["supply_desc"])
    typ = _by_index(session.walk(TABLES["supply_type"]), TABLES["supply_type"])
    cls = _by_index(session.walk(TABLES["supply_class"]), TABLES["supply_class"])
    lvl = _by_index(session.walk(TABLES["supply_level"]), TABLES["supply_level"])
    mx = _by_index(session.walk(TABLES["supply_max"]), TABLES["supply_max"])
    unit = _by_index(session.walk(TABLES["supply_unit"]), TABLES["supply_unit"])
    out = [
        Supply.from_snmp(
            name=_s(desc.get(idx)) or f"supply {idx}",
            type=_supply_type(_i(typ.get(idx))),
            class_=_supply_class(_i(cls.get(idx))),
            level=_i(lvl.get(idx)),
            max=_i(mx.get(idx)),
            unit=_i(unit.get(idx)),
        )
        for idx in _sorted_indices(desc, lvl, mx)
    ]
    return tuple(out)


def _read_trays(session: Session) -> Tuple[Tray, ...]:
    name = _by_index(session.walk(TABLES["input_name"]), TABLES["input_name"])
    media = _by_index(session.walk(TABLES["input_media"]), TABLES["input_media"])
    cur = _by_index(session.walk(TABLES["input_current"]), TABLES["input_current"])
    mx = _by_index(session.walk(TABLES["input_max"]), TABLES["input_max"])
    status = _by_index(session.walk(TABLES["input_status"]), TABLES["input_status"])
    out = [
        Tray.from_snmp(
            name=_s(name.get(idx)) or f"tray {idx}",
            media=_s(media.get(idx)),
            level=_i(cur.get(idx)),
            max=_i(mx.get(idx)),
            status=_i(status.get(idx)),
        )
        for idx in _sorted_indices(name, cur, mx)
    ]
    return tuple(out)


def _read_errors(session: Session) -> Tuple[PrinterError, ...]:
    desc = _by_index(session.walk(TABLES["prt_alert_desc"]), TABLES["prt_alert_desc"])
    code = _by_index(session.walk(TABLES["prt_alert_code"]), TABLES["prt_alert_code"])
    out = []
    for idx in _sorted_indices(desc, code):
        text = _s(desc.get(idx))
        num = _i(code.get(idx))
        if text is None and num is None:
            continue
        out.append(PrinterError(code=num, description=text or ""))  # code None = отсутствовал
    return tuple(out)


def _read_status(session: Session) -> Optional[str]:
    # Берём первый валидный hrPrinterStatus (низший hrDeviceIndex); для тандемных/
    # многомоторных принтеров это статус первого мотора — упрощение для v1.
    for val in session.walk(TABLES["hr_printer_status"]).values():
        code = _i(val)
        if code is not None:
            return _PRINTER_STATUS.get(code, "unknown")
    return None


def read(session: Session, *, ip: str = "") -> PrinterReading:
    """Собрать PrinterReading. ip передаёт вызывающий (collector)."""
    scal = session.get(
        [
            STANDARD["sys_descr"],
            STANDARD["sys_object_id"],
            STANDARD["sys_uptime"],
            STANDARD["sys_name"],
            STANDARD["prt_serial"],
            STANDARD["prt_marker_life_count"],
        ]
    )
    sys_object_id = _s(scal.get(STANDARD["sys_object_id"]))
    vendor = oids.vendor_for_sysobjectid(sys_object_id) if sys_object_id else None
    return PrinterReading(
        ip=ip,
        hostname=_s(scal.get(STANDARD["sys_name"])),
        vendor=vendor,
        model=_s(scal.get(STANDARD["sys_descr"])),
        serial=_s(scal.get(STANDARD["prt_serial"])),
        uptime=_i(scal.get(STANDARD["sys_uptime"])),
        status=_read_status(session),
        total_pages=_i(scal.get(STANDARD["prt_marker_life_count"])),
        supplies=_read_supplies(session),
        trays=_read_trays(session),
        errors=_read_errors(session),
        source_protocol="snmp",
    )
