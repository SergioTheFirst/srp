"""Phase 2 — оркестрация probe(ip): классификация → драйвер → PrinterReading."""

from server.printers import collector
from server.printers.oids import STANDARD


class FakeSession:
    def __init__(self, scalars: dict, tables: dict = None) -> None:
        self._scalars = scalars
        self._tables = tables or {}

    def get(self, oid_list: list) -> dict:
        return {o: self._scalars[o] for o in oid_list if o in self._scalars}

    def walk(self, base_oid: str) -> dict:
        return self._tables.get(base_oid, {})


def test_probe_returns_reading_for_printer():
    sess = FakeSession(
        {
            STANDARD["prt_serial"]: "CN1",  # ветка Printer-MIB .43 → принтер
            STANDARD["sys_object_id"]: "1.3.6.1.4.1.11.2.3.9.1",
        }
    )
    r = collector.probe("192.168.1.50", session=sess)
    assert r is not None
    assert r.ip == "192.168.1.50" and r.serial == "CN1" and r.vendor == "hp"


def test_probe_returns_none_for_non_printer():
    sess = FakeSession({STANDARD["sys_descr"]: "Windows Server 2019"})
    assert collector.probe("192.168.1.10", session=sess) is None


def test_probe_classifies_via_hr_device_type():
    sess = FakeSession({"1.3.6.1.2.1.25.3.2.1.2.1": "1.3.6.1.2.1.25.3.1.5"})
    r = collector.probe("192.168.1.51", session=sess)
    assert r is not None and r.ip == "192.168.1.51"
