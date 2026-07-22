"""Phase 4 — printer storage: append-only readings + latest inventory + series.

``printers`` holds one latest-inventory row per printer (keyed by the stable
identity, serial > MAC > IP); ``printer_readings`` is the append-only time series
(scalar counters + a JSON detail blob of supplies/trays/errors). These pin the
store/get contract the poll scheduler writes through.

Pure SQLite; no network, no FastAPI.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def db_init(tmp_path):
    from server import db

    db.init_db(tmp_path / "t.db", retain_printer_readings=5)
    return db


def _reading(ip: str = "192.168.1.50", *, online: bool = True, **kw):
    base = {
        "ip": ip,
        "online": online,
        "hostname": "PRN-1",
        "mac": "AA-BB-CC-DD-EE-01",
        "vendor": "hp",
        "model": "HP LaserJet",
        "serial": "CNX-1",
        "firmware": "1.0",
        "uptime": 1000,
        "status": "idle",
        "total_pages": 12000,
        "color_pages": None,
        "mono_pages": None,
        "duplex_pages": None,
        "supplies": [
            {
                "name": "Black",
                "type": "toner",
                "class_": "consumed",
                "level": 20,
                "max": 100,
                "percent": 20,
                "unit": 4,
            }
        ],
        "trays": [],
        "errors": [],
        "source_protocol": "snmp",
        "sources": ["spooler"],
    }
    base.update(kw)
    return base


def test_store_get_round_trip(db_init):
    db = db_init
    db.store_printer_reading("prn-sn-CNX-1", _reading())
    p = db.get_printer("prn-sn-CNX-1")
    assert p is not None
    assert p["ip"] == "192.168.1.50"
    assert p["total_pages"] == 12000
    assert p["vendor"] == "hp"
    assert p["status"] == "idle"
    assert p["online"] is True
    assert len(p["supplies"]) == 1 and p["supplies"][0]["percent"] == 20


def test_get_printer_absent_returns_none(db_init):
    assert db_init.get_printer("nope") is None


def _arp_shell(ip: str, mac: str):
    # A bare ARP neighbour that never answered as a printer: no model/serial/pages,
    # discovered only via "arp".
    return {
        "ip": ip,
        "online": False,
        "hostname": None,
        "mac": mac,
        "vendor": None,
        "model": None,
        "serial": None,
        "firmware": None,
        "uptime": None,
        "status": "unreachable",
        "total_pages": None,
        "color_pages": None,
        "mono_pages": None,
        "duplex_pages": None,
        "supplies": [],
        "trays": [],
        "errors": [],
        "source_protocol": None,
        "sources": ["arp"],
    }


def test_printer_is_confirmed(db_init):
    db = db_init
    db.store_printer_reading("prn-sn-CNX-1", _reading())
    db.store_printer_reading(
        "prn-mac-AABBCCDDEE77", _arp_shell("192.168.1.77", "AA-BB-CC-DD-EE-77")
    )
    assert db.printer_is_confirmed("prn-sn-CNX-1") is True
    assert db.printer_is_confirmed("prn-mac-AABBCCDDEE77") is False
    assert db.printer_is_confirmed("nope") is False


def test_delete_unconfirmed_arp_printers(db_init):
    db = db_init
    db.store_printer_reading("prn-sn-CNX-1", _reading())  # real printer
    db.store_printer_reading(
        "prn-mac-AABBCCDDEE77", _arp_shell("192.168.1.77", "AA-BB-CC-DD-EE-77")
    )
    db.store_printer_reading(
        "prn-mac-AABBCCDDEE88", _arp_shell("192.168.1.88", "AA-BB-CC-DD-EE-88")
    )
    removed = db.delete_unconfirmed_arp_printers()
    assert removed == 2
    ids = [r["printer_id"] for r in db.get_printers()]
    assert ids == ["prn-sn-CNX-1"]  # only the real printer survives


def test_delete_unconfirmed_arp_printers_nulls_net_link(db_init):
    """Purging a phantom printer clears any net_devices->printer soft FK but keeps
    the network node (symmetric with delete_device's agent-FK clearing)."""
    db = db_init
    db.store_printer_reading(
        "prn-mac-AABBCCDDEE77", _arp_shell("192.168.1.77", "AA-BB-CC-DD-EE-77")
    )
    db.upsert_net_device({"device_nid": "nd-mac-AABBCCDDEE77", "mac": "AA-BB-CC-DD-EE-77"})
    db.set_net_device_links("nd-mac-AABBCCDDEE77", printer_id="prn-mac-AABBCCDDEE77")

    assert db.delete_unconfirmed_arp_printers() == 1

    row = db.get_net_device("nd-mac-AABBCCDDEE77")
    assert row is not None  # the network node survives the printer purge
    assert row["printer_id"] is None  # the dangling printer FK is cleared


def test_overview_hides_unconfirmed_arp_printers(db_init):
    db = db_init
    db.store_printer_reading("prn-sn-CNX-1", _reading())
    db.store_printer_reading(
        "prn-mac-AABBCCDDEE77", _arp_shell("192.168.1.77", "AA-BB-CC-DD-EE-77")
    )
    ov = db.get_printers_overview(days=30)
    ids = [p["printer_id"] for p in ov["printers"]]
    assert ids == ["prn-sn-CNX-1"]


def test_get_printers_lists_each_printer_once(db_init):
    db = db_init
    db.store_printer_reading("prn-sn-A", _reading(ip="192.168.1.10", serial="A"))
    db.store_printer_reading("prn-sn-A", _reading(ip="192.168.1.10", serial="A", total_pages=12050))
    db.store_printer_reading("prn-sn-B", _reading(ip="192.168.1.11", serial="B"))
    rows = db.get_printers()
    ids = [r["printer_id"] for r in rows]
    assert ids.count("prn-sn-A") == 1 and ids.count("prn-sn-B") == 1
    a = next(r for r in rows if r["printer_id"] == "prn-sn-A")
    assert a["total_pages"] == 12050  # latest wins
    assert a["low_supply_pct"] == 20  # consumed toner at 20%


def test_series_is_append_only_newest_first(db_init):
    db = db_init
    for pages in (100, 200, 300):
        db.store_printer_reading("prn-sn-A", _reading(serial="A", total_pages=pages))
    series = db.get_printer_series("prn-sn-A", limit=10)
    assert [s["total_pages"] for s in series] == [300, 200, 100]


def test_pages_series_overview_returns_per_printer_history(db_init):
    db = db_init
    for i, pages in enumerate((12000, 12010, 12025)):
        db.store_printer_reading(
            "prn-sn-A",
            _reading(serial="A", total_pages=pages),
            received_at=f"2026-06-2{i}T10:00:00+00:00",
        )
    db.store_printer_reading("prn-sn-B", _reading(ip="192.168.1.11", serial="B", total_pages=500))
    db.store_printer_reading(  # NULL counter (unreachable) must not be plotted as a point
        "prn-sn-A", _reading(serial="A", total_pages=None), received_at="2026-06-24T10:00:00+00:00"
    )
    out = db.get_printers_pages_series(days=0)
    by_id = {s["printer_id"]: s for s in out}
    a = by_id["prn-sn-A"]
    assert [p["total_pages"] for p in a["points"]] == [
        12000,
        12010,
        12025,
    ]  # ascending, NULL skipped
    assert a["label"]  # human label present for the chart legend
    assert by_id["prn-sn-B"]["points"][-1]["total_pages"] == 500


def test_pages_series_includes_active_printer_outside_top_by_lifetime(db_init):
    """P2-3: a printer with recent activity must not be silently dropped from
    the trend chart just because a higher lifetime-total printer that's gone
    quiet occupies its top-N slot instead."""
    from datetime import datetime, timedelta, timezone

    db = db_init
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    db.store_printer_reading(
        "prn-high", _reading(ip="192.168.1.20", serial="HIGH", total_pages=50000), received_at=old
    )
    db.store_printer_reading(
        "prn-med", _reading(ip="192.168.1.21", serial="MED", total_pages=30000), received_at=recent
    )
    db.store_printer_reading(
        "prn-low", _reading(ip="192.168.1.22", serial="LOW", total_pages=100), received_at=recent
    )

    out = db.get_printers_pages_series(days=30, max_printers=1)
    ids = {s["printer_id"] for s in out}
    assert "prn-med" in ids  # active in-window -- must not be dropped
    assert "prn-high" not in ids  # top by lifetime, but no readings in the window
    # 3 candidates (high/med/low) compete for a max_printers*2=2 cap -- pins that
    # the cap itself still applies, not just the "no readings in window" drop.
    assert "prn-low" not in ids
    assert len(out) == 1


def test_readings_retention_caps_per_printer(db_init):
    db = db_init  # retain_printer_readings=5
    for i in range(8):
        db.store_printer_reading("prn-sn-A", _reading(serial="A", total_pages=i))
    series = db.get_printer_series("prn-sn-A", limit=100)
    assert len(series) == 5
    assert [s["total_pages"] for s in series] == [7, 6, 5, 4, 3]


def test_first_seen_preserved_last_seen_advances(db_init):
    db = db_init
    db.store_printer_reading(
        "prn-sn-A", _reading(serial="A"), received_at="2026-06-19T10:00:00+00:00"
    )
    db.store_printer_reading(
        "prn-sn-A", _reading(serial="A"), received_at="2026-06-19T11:00:00+00:00"
    )
    p = db.get_printer("prn-sn-A")
    assert p["first_seen"] == "2026-06-19T10:00:00+00:00"
    assert p["last_seen"] == "2026-06-19T11:00:00+00:00"


def test_unreachable_poll_keeps_known_inventory_but_marks_offline(db_init):
    db = db_init
    db.store_printer_reading("prn-sn-A", _reading(serial="A", total_pages=12000, vendor="hp"))
    # Next poll: host did not answer -> minimal reading, no vendor/serial/pages.
    db.store_printer_reading(
        "prn-sn-A",
        {
            "ip": "192.168.1.50",
            "online": False,
            "status": "unreachable",
            "serial": None,
            "vendor": None,
            "model": None,
            "mac": None,
            "hostname": None,
            "total_pages": None,
            "sources": ["spooler"],
        },
    )
    p = db.get_printer("prn-sn-A")
    assert p["status"] == "unreachable"
    assert p["online"] is False
    assert p["vendor"] == "hp"  # COALESCE keeps last-known identity
    assert p["total_pages"] == 12000  # keeps last-known counter


def test_migration_idempotent_and_tables_present(db_init, tmp_path):
    db = db_init
    # Re-running init_db on the same path is a no-op (CREATE TABLE IF NOT EXISTS).
    db.init_db(tmp_path / "t.db", retain_printer_readings=5)
    with db._connect() as conn:
        names = {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"printers", "printer_readings"} <= names


# --------------------------------------------------------------------------- #
# printer_ipp_jobs -- supplementary completed-job cache from IPP Get-Jobs
# --------------------------------------------------------------------------- #


def test_store_and_get_printer_ipp_jobs(db_init):
    db = db_init
    jobs = [{"job_id": 7, "name": "doc-A", "user_name": "ivanov", "impressions": 3}]
    db.store_printer_ipp_jobs("prn-1", jobs, received_at="2026-07-02T10:00:00+00:00")
    got = db.get_printer_ipp_jobs("prn-1")
    assert len(got) == 1
    assert got[0]["job_id"] == 7
    assert got[0]["user_name"] == "ivanov"
    assert got[0]["name"] == "doc-A"
    assert got[0]["impressions"] == 3


def test_store_printer_ipp_jobs_upsert_is_idempotent(db_init):
    db = db_init
    jobs = [{"job_id": 7, "name": "doc-A", "user_name": "ivanov", "impressions": 3}]
    db.store_printer_ipp_jobs("prn-1", jobs, received_at="2026-07-02T10:00:00+00:00")
    db.store_printer_ipp_jobs("prn-1", jobs, received_at="2026-07-02T10:15:00+00:00")
    assert db.count_printer_ipp_jobs("prn-1") == 1


def test_store_printer_ipp_jobs_coalesce_preserves_known_fields(db_init):
    """A later sweep re-reporting the same job_id with a blank field must not
    wipe a previously-known value (mirrors printer_readings COALESCE identity)."""
    db = db_init
    db.store_printer_ipp_jobs(
        "prn-1",
        [{"job_id": 7, "name": "doc-A", "user_name": "ivanov", "impressions": 3}],
        received_at="2026-07-02T10:00:00+00:00",
    )
    db.store_printer_ipp_jobs(
        "prn-1",
        [{"job_id": 7, "name": None, "user_name": None, "impressions": None}],
        received_at="2026-07-02T10:15:00+00:00",
    )
    got = db.get_printer_ipp_jobs("prn-1")
    assert got[0]["user_name"] == "ivanov"  # not wiped by the later None


def test_store_printer_ipp_jobs_prunes_beyond_keep_cap(db_init):
    db = db_init
    many = [{"job_id": i, "name": None, "user_name": None, "impressions": None} for i in range(250)]
    db.store_printer_ipp_jobs("prn-1", many, received_at="2026-07-02T11:00:00+00:00")
    assert db.count_printer_ipp_jobs("prn-1") <= 200


def test_get_printer_ipp_jobs_empty_for_unknown_printer(db_init):
    assert db_init.get_printer_ipp_jobs("nope") == []


def test_store_printer_ipp_jobs_noop_on_empty_list(db_init):
    db = db_init
    db.store_printer_ipp_jobs("prn-1", [], received_at="2026-07-02T10:00:00+00:00")
    assert db.get_printer_ipp_jobs("prn-1") == []
