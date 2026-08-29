"""Phase 0 — единая база OID принтеров: структура, отсутствие дублей, vendor-резолвинг."""

from server.printers import oids


def test_standard_oids_present_and_typed():
    # Базовый Printer-MIB / MIB-II / HOST-RESOURCES — обязательны.
    assert oids.STANDARD["sys_descr"] == "1.3.6.1.2.1.1.1.0"
    assert oids.STANDARD["sys_object_id"] == "1.3.6.1.2.1.1.2.0"
    assert oids.STANDARD["sys_uptime"] == "1.3.6.1.2.1.1.3.0"
    assert oids.STANDARD["sys_name"] == "1.3.6.1.2.1.1.5.0"
    assert oids.STANDARD["prt_serial"] == "1.3.6.1.2.1.43.5.1.1.17.1"
    assert oids.STANDARD["prt_marker_life_count"] == "1.3.6.1.2.1.43.10.2.1.4.1.1"
    # Табличные базы (без индекса) — для walk.
    assert oids.TABLES["supply_level"] == "1.3.6.1.2.1.43.11.1.1.9"
    assert oids.TABLES["supply_max"] == "1.3.6.1.2.1.43.11.1.1.8"
    assert oids.TABLES["supply_desc"] == "1.3.6.1.2.1.43.11.1.1.6"
    assert oids.TABLES["input_current"] == "1.3.6.1.2.1.43.8.2.1.10"
    assert oids.TABLES["hr_printer_status"] == "1.3.6.1.2.1.25.3.5.1.1"


def test_no_duplicate_oid_strings_within_a_group():
    for group in (oids.STANDARD, oids.TABLES):
        values = list(group.values())
        assert len(values) == len(set(values))


def test_vendor_enterprise_map_resolves_prefix():
    # sysObjectID -> вендор по enterprise-префиксу 1.3.6.1.4.1.<N>
    assert oids.vendor_for_sysobjectid("1.3.6.1.4.1.11.2.3.9.1") == "hp"
    assert oids.vendor_for_sysobjectid("1.3.6.1.4.1.1347.1") == "kyocera"
    assert oids.vendor_for_sysobjectid("1.3.6.1.4.1.99999") is None
    # sysObjectID вне enterprise-ветки (1.3.6.1.4.1) → None, без выдумки вендора.
    assert oids.vendor_for_sysobjectid("1.3.6.1.2.1.1.2.0") is None
