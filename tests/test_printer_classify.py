"""Phase 2 — классификатор «это принтер?»: только определительные сигналы."""

from server.printers import classify


def test_printer_mib_response_is_printer():
    assert classify.is_printer({"1.3.6.1.2.1.43.5.1.1.17.1": "ABC123"}) is True


def test_hr_device_type_printer_is_printer():
    # hrDeviceType колонка вернула hrDevicePrinter (1.3.6.1.2.1.25.3.1.5).
    probe = {"1.3.6.1.2.1.25.3.2.1.2.1": "1.3.6.1.2.1.25.3.1.5"}
    assert classify.is_printer(probe) is True


def test_pc_without_printer_signal_is_not_printer():
    pc = {
        "1.3.6.1.2.1.1.1.0": "Windows Server",
        "1.3.6.1.2.1.1.2.0": "1.3.6.1.4.1.311.1.1.3.1.1",  # Microsoft enterprise
    }
    assert classify.is_printer(pc) is False


def test_vendor_enterprise_alone_is_not_printer():
    # HP enterprise sysObjectID без Printer-MIB (напр. HP-сервер) → НЕ принтер.
    assert classify.is_printer({"1.3.6.1.2.1.1.2.0": "1.3.6.1.4.1.11.2.3.9.1"}) is False


def test_single_open_port_no_snmp_is_not_printer():
    assert classify.is_printer({}) is False


def test_ipp_marker_forces_printer():
    assert classify.is_printer({}, ipp=True) is True


def test_printer_mib_with_none_value_is_not_enough():
    # Ветка ответила исключением (noSuchObject → None) → не принтер.
    assert classify.is_printer({"1.3.6.1.2.1.43.5.1.1.17.1": None}) is False


def test_hr_device_type_false_positive_sibling_oid_rejected():
    # Defense-in-depth: hrDeviceType check must use dot-boundary like Printer-MIB does.
    # A sibling OID that shares the numeric prefix (e.g., .20 instead of .2) must
    # NOT falsely match via bare startswith. This was a vulnerability before the fix.
    sibling_oid = "1.3.6.1.2.1.25.3.2.1.20"  # looks like hrDeviceType.0 but isn't
    probe = {sibling_oid: "1.3.6.1.2.1.25.3.1.5"}  # has printer value
    assert classify.is_printer(probe) is False  # should reject sibling, not match
