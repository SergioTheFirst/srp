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
    # Inject no-op fallbacks so the unit test never touches the real network.
    assert (
        collector.probe(
            "192.168.1.10",
            session=sess,
            ipp_fn=lambda *a, **k: None,
            http_fn=lambda *a, **k: None,
        )
        is None
    )


def test_probe_classifies_via_hr_device_type():
    sess = FakeSession({"1.3.6.1.2.1.25.3.2.1.2.1": "1.3.6.1.2.1.25.3.1.5"})
    r = collector.probe(
        "192.168.1.51", session=sess, ipp_fn=lambda *a, **k: None, http_fn=lambda *a, **k: None
    )
    assert r is not None and r.ip == "192.168.1.51"


def test_probe_falls_back_to_ipp_when_snmp_silent():
    from server.printers.models import PrinterReading

    sess = FakeSession({})  # SNMP says nothing -> not classified a printer
    r = collector.probe(
        "192.168.1.60",
        session=sess,
        ipp_fn=lambda ip, **k: PrinterReading(ip=ip, model="HP via IPP", source_protocol="ipp"),
        http_fn=lambda *a, **k: None,
    )
    assert r is not None and r.source_protocol == "ipp" and r.model == "HP via IPP"


def test_probe_falls_back_to_http_when_ipp_none():
    from server.printers.models import PrinterReading

    sess = FakeSession({})
    r = collector.probe(
        "192.168.1.61",
        session=sess,
        ipp_fn=lambda *a, **k: None,
        http_fn=lambda ip, **k: PrinterReading(ip=ip, model="Web printer", source_protocol="http"),
    )
    assert r is not None and r.source_protocol == "http"


def test_probe_snmp_printer_skips_fallbacks():
    # A confirmed SNMP printer must NOT trigger IPP/HTTP probes.
    called = []
    sess = FakeSession({STANDARD["prt_serial"]: "CN9"})
    r = collector.probe(
        "192.168.1.62",
        session=sess,
        ipp_fn=lambda *a, **k: called.append("ipp"),
        http_fn=lambda *a, **k: called.append("http"),
    )
    assert r is not None and r.serial == "CN9" and called == []
