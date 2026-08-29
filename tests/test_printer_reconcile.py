"""Phase 6 — hardware<->software print reconcile + overview/detail composition."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def db_init(tmp_path):
    from server import db

    db.init_db(tmp_path / "t.db")
    return db


def _hw(ip: str, serial: str) -> dict:
    return {
        "ip": ip,
        "online": True,
        "status": "idle",
        "model": "HP LaserJet",
        "hostname": "PRN1",
        "serial": serial,
        "total_pages": 9000,
        "supplies": [],
        "trays": [],
        "errors": [],
        "sources": ["spooler"],
    }


# --- pure matcher ---------------------------------------------------------- #
def test_match_software_by_ip_in_name():
    from server import db

    hw = {"ip": "192.168.1.50", "hostname": "PRN1", "model": "HP LJ"}
    sw = [{"name": "HP LaserJet @ 192.168.1.50", "pages": 10}]
    assert db._match_software(hw, sw) is sw[0]


def test_match_software_by_hostname():
    from server import db

    hw = {"ip": "192.168.1.50", "hostname": "PRN1", "model": ""}
    sw = [{"name": "PRN1", "pages": 5}]
    assert db._match_software(hw, sw)["name"] == "PRN1"


def test_match_software_none_when_no_overlap():
    from server import db

    hw = {"ip": "192.168.1.50", "hostname": "PRN1", "model": "HP LaserJet"}
    sw = [{"name": "Canon в бухгалтерии", "pages": 5}]
    assert db._match_software(hw, sw) is None


def test_match_software_ip_not_prefix_of_longer():
    from server import db

    hw = {"ip": "192.168.1.5", "hostname": "", "model": ""}
    sw = [{"name": "Printer @ 192.168.1.50", "pages": 9}]
    assert db._match_software(hw, sw) is None  # .5 must not match inside .50


def test_match_software_ip_whole_token_matches():
    from server import db

    hw = {"ip": "192.168.1.5", "hostname": "", "model": ""}
    sw = [{"name": "Printer @ 192.168.1.5", "pages": 9}]
    assert db._match_software(hw, sw)["pages"] == 9


# --- overview / detail (DB) ------------------------------------------------ #
def test_overview_matches_software_by_ip(db_init):
    db = db_init
    db.store_printer_reading("prn-sn-A", _hw("192.168.1.50", "A"))
    db.store_print_jobs(
        "dev-1",
        [{"job_id": 1, "ts": "2026-06-19T10:00:00Z", "printer": "HP @ 192.168.1.50", "pages": 7}],
    )
    ov = db.get_printers_overview(days=0)
    p = ov["printers"][0]
    assert p["software"] is not None and p["software"]["pages"] == 7
    assert p["software"]["device_count"] == 1
    assert ov["unmatched_software"] == []


def test_overview_lists_unmatched_software(db_init):
    db = db_init
    db.store_printer_reading("prn-sn-A", _hw("192.168.1.50", "A"))
    db.store_print_jobs(
        "dev-1",
        [{"job_id": 1, "ts": "2026-06-19T10:00:00Z", "printer": "Canon в бухгалтерии", "pages": 3}],
    )
    ov = db.get_printers_overview(days=0)
    assert ov["printers"][0]["software"] is None
    assert len(ov["unmatched_software"]) == 1 and ov["unmatched_software"][0]["pages"] == 3


def test_get_printer_detail_has_series_and_software(db_init):
    db = db_init
    db.store_printer_reading("prn-sn-A", _hw("192.168.1.50", "A"))
    db.store_printer_reading("prn-sn-A", _hw("192.168.1.50", "A"))
    db.store_print_jobs(
        "dev-1",
        [{"job_id": 1, "ts": "2026-06-19T10:00:00Z", "printer": "HP 192.168.1.50", "pages": 7}],
    )
    d = db.get_printer_detail("prn-sn-A", days=0)
    assert len(d["series"]) == 2
    assert d["software"]["pages"] == 7
    assert d["software"]["devices"][0]["pages"] == 7
    assert db.get_printer_detail("nope", days=0) is None
