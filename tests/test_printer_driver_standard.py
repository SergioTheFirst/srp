"""Phase 2 — generic Printer-MIB драйвер + реестр драйверов."""

from server.printers import drivers, oids
from server.printers.drivers import standard

O_STD = oids.STANDARD
O_TAB = oids.TABLES


class FakeSession:
    """Сессия-заглушка: get отдаёт скаляры, walk — табличные ветки по базе."""

    def __init__(self, scalars: dict, tables: dict) -> None:
        self._scalars = scalars
        self._tables = tables

    def get(self, oid_list: list) -> dict:
        return {o: self._scalars[o] for o in oid_list if o in self._scalars}

    def walk(self, base_oid: str) -> dict:
        return self._tables.get(base_oid, {})


def _full_printer_session() -> FakeSession:
    scalars = {
        O_STD["sys_descr"]: "HP LaserJet M607",
        O_STD["sys_object_id"]: "1.3.6.1.4.1.11.2.3.9.1",
        O_STD["sys_uptime"]: 123456,
        O_STD["sys_name"]: "PRN-FLOOR2",
        O_STD["prt_serial"]: "CNB1234567",
        O_STD["prt_marker_life_count"]: 84210,
    }
    tables = {
        O_TAB["supply_desc"]: {O_TAB["supply_desc"] + ".1.1": "Black Cartridge"},
        O_TAB["supply_type"]: {O_TAB["supply_type"] + ".1.1": 3},
        O_TAB["supply_class"]: {O_TAB["supply_class"] + ".1.1": 3},  # consumed
        O_TAB["supply_level"]: {O_TAB["supply_level"] + ".1.1": 200},
        O_TAB["supply_max"]: {O_TAB["supply_max"] + ".1.1": 1000},
        O_TAB["supply_unit"]: {O_TAB["supply_unit"] + ".1.1": 4},
        O_TAB["input_name"]: {O_TAB["input_name"] + ".1": "Tray 1"},
        O_TAB["input_media"]: {O_TAB["input_media"] + ".1": "A4"},
        O_TAB["input_current"]: {O_TAB["input_current"] + ".1": 250},
        O_TAB["input_max"]: {O_TAB["input_max"] + ".1": 500},
        O_TAB["input_status"]: {O_TAB["input_status"] + ".1": 0},
        O_TAB["prt_alert_desc"]: {O_TAB["prt_alert_desc"] + ".1": "Toner low"},
        O_TAB["prt_alert_code"]: {O_TAB["prt_alert_code"] + ".1": 1101},
        O_TAB["hr_printer_status"]: {O_TAB["hr_printer_status"] + ".1": 3},
    }
    return FakeSession(scalars, tables)


def test_standard_read_assembles_full_reading():
    r = standard.read(_full_printer_session(), ip="192.168.1.50")
    assert r.ip == "192.168.1.50"
    assert r.vendor == "hp"
    assert r.model == "HP LaserJet M607"
    assert r.serial == "CNB1234567"
    assert r.hostname == "PRN-FLOOR2"
    assert r.uptime == 123456
    assert r.total_pages == 84210
    assert r.status == "idle"
    assert len(r.supplies) == 1
    s = r.supplies[0]
    assert s.name == "Black Cartridge" and s.type == "toner" and s.percent == 20
    assert s.class_ == "consumed"
    assert len(r.trays) == 1 and r.trays[0].media == "A4" and r.trays[0].level == 250
    assert len(r.errors) == 1
    assert r.errors[0].code == 1101 and "Toner" in r.errors[0].description


def test_standard_read_unreachable_is_empty_reading():
    r = standard.read(FakeSession({}, {}), ip="192.168.1.99")
    assert r.ip == "192.168.1.99"
    assert r.serial is None and r.vendor is None and r.status is None
    assert r.supplies == () and r.trays == () and r.errors == ()


def test_get_driver_falls_back_to_standard():
    # Phase 5: a known vendor now resolves to its vendor driver; unknown/garbage
    # still falls back to the generic standard reader.
    assert drivers.get_driver("1.3.6.1.4.1.11.2.3.9.1") is not standard.read  # HP → vendor driver
    assert drivers.get_driver("1.3.6.1.4.1.99999.1") is standard.read
    assert drivers.get_driver("garbage") is standard.read
    assert drivers.get_driver(None) is standard.read
