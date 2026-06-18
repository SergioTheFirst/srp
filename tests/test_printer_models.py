"""Phase 2 — неизменяемые модели принтера: нормализация сентинелов, percent."""

import pytest
from server.printers import models


def test_supply_percent_from_level_and_max():
    s = models.Supply.from_snmp(
        name="Black", type="toner", level=750, max=1000, unit=4, class_="consumed"
    )
    assert s.percent == 75
    assert s.level == 750 and s.max == 1000
    assert s.class_ == "consumed"


def test_supply_sentinels_become_none():
    # Printer-MIB: -3 some-remaining, -2 unknown → UNKNOWN, не выдумываем число.
    s = models.Supply.from_snmp(name="Black", type="toner", level=-3, max=1000)
    assert s.level is None and s.percent is None
    s2 = models.Supply.from_snmp(name="Drum", type="drum", level=500, max=-2)
    assert s2.max is None and s2.percent is None  # max неизвестен → percent неизвестен


def test_supply_percent_clamped_0_100():
    s = models.Supply.from_snmp(name="X", type="toner", level=1200, max=1000)
    assert s.percent == 100
    empty = models.Supply.from_snmp(name="Y", type="toner", level=0, max=1000)
    assert empty.percent == 0


def test_tray_normalizes_sentinels_keeps_media():
    t = models.Tray.from_snmp(name="Tray 1", media="A4", level=-2, max=500, status=3)
    assert t.level is None and t.max == 500 and t.media == "A4" and t.status == 3


def test_printer_reading_frozen_with_defaults():
    r = models.PrinterReading(ip="192.168.1.9")
    assert r.supplies == () and r.trays == () and r.errors == ()
    assert r.source_protocol == "snmp" and r.status is None
    with pytest.raises(AttributeError):
        r.ip = "x"  # type: ignore[misc]  # frozen


def test_printer_error_holds_code_and_description():
    e = models.PrinterError(code=1, description="нет бумаги")
    assert e.code == 1 and e.description == "нет бумаги"
    e2 = models.PrinterError(code=None, description="замятие")  # код может отсутствовать
    assert e2.code is None
