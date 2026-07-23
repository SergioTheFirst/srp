"""P2-15: `days` self-clamp + P0-7-style bound cutoff for 5 print/printer query
functions in server/db.py (get_printers_pages_series, get_printer_print_summary,
get_device_print, get_fleet_print, get_print_analytics).

Two defects, one patch:

1. ``days`` used to be interpolated into SQL text via an f-string, safe today
   only because every HTTP caller pre-clamps it in server/api.py *before*
   reaching db.py. These tests call the db.py functions DIRECTLY (bypassing
   server/api.py) to prove the module now defends itself regardless of caller
   discipline.
2. The cutoff itself used to be built as raw SQL text (`datetime('now',
   '-{days} days')`), a space-separated string compared lexicographically
   against the T-separated stored `ts`/`received_at` -- wrongly including
   rows on the last calendar day of the window (the same P0-7 bug class,
   already fixed elsewhere via db._cutoff_iso; P2-15 extends that fix here).

Pure SQLite; no network, no FastAPI.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def db_init(tmp_path):
    from server import db

    db.init_db(tmp_path / "t.db")
    return db


def _reading(total_pages, **kw) -> dict:
    base = {
        "ip": "192.168.1.50",
        "online": True,
        "hostname": "PRN-1",
        "mac": "AA-BB-CC-DD-EE-01",
        "vendor": "hp",
        "model": "HP LaserJet",
        "serial": "S1",
        "firmware": "1.0",
        "uptime": 1000,
        "status": "idle",
        "total_pages": total_pages,
        "color_pages": None,
        "mono_pages": None,
        "duplex_pages": None,
        "supplies": [],
        "trays": [],
        "errors": [],
        "source_protocol": "snmp",
        "sources": ["spooler"],
    }
    base.update(kw)
    return base


def _same_day_but_expired(days: int, buffer_hours: int = 1) -> str:
    """A timestamp `buffer_hours` before the `days`-cutoff -- shares the
    cutoff's calendar date but is genuinely outside the window. Before the
    P0-7-style fix, SQLite's TEXT comparison ('T' 0x54 > ' ' 0x20) wrongly
    included rows like this whenever the calendar date matched the cutoff's."""
    return (datetime.now(timezone.utc) - timedelta(days=days, hours=buffer_hours)).isoformat()


# --------------------------------------------------------------------------- #
# Part 1 (P2-15 main): `days` is no longer trusted as a pre-clamped int. A
# direct caller (bypassing server/api.py's _clamp_days) that passes a
# malicious/non-int value must get a controlled ValueError/TypeError at the
# function's entry, before any SQL is built -- never a raw string reaching
# SQL text.
# --------------------------------------------------------------------------- #

_MALICIOUS_DAYS = "0) UNION SELECT sql FROM sqlite_master--"


def test_pages_series_rejects_malicious_days_string(db_init):
    with pytest.raises(ValueError):
        db_init.get_printers_pages_series(days=_MALICIOUS_DAYS)  # type: ignore[arg-type]


def test_printer_summary_rejects_malicious_days_string(db_init):
    with pytest.raises(ValueError):
        db_init.get_printer_print_summary(days=_MALICIOUS_DAYS)  # type: ignore[arg-type]


def test_device_print_rejects_malicious_days_string(db_init):
    with pytest.raises(ValueError):
        db_init.get_device_print("dev-1", days=_MALICIOUS_DAYS)  # type: ignore[arg-type]


def test_fleet_print_rejects_malicious_days_string(db_init):
    with pytest.raises(ValueError):
        db_init.get_fleet_print(days=_MALICIOUS_DAYS)  # type: ignore[arg-type]


def test_print_analytics_rejects_malicious_days_string(db_init):
    with pytest.raises(ValueError):
        db_init.get_print_analytics(days=_MALICIOUS_DAYS)  # type: ignore[arg-type]


class _EvilDays:
    """A plain malicious *string* only proves half the bug: `"str" > 0` in
    Python 3 always raises TypeError, so a naive `if days > 0:` gate already
    stops it (accidentally, not by design). A caller-supplied object that
    overrides `__gt__` sails past that gate, and its `__str__` is what an
    f-string embeds into SQL text -- proving the pre-fix gap was a real
    injection primitive, not just an unrelated stray exception."""

    def __gt__(self, other: object) -> bool:
        return True

    def __str__(self) -> str:
        # Closes `datetime('now', '-...days')` cleanly, then `OR (1=1)` makes
        # the whole WHERE clause vacuously true, then `--` comments out the
        # unbalanced trailing template text (` days')`).
        return "9999 days') OR (1=1) --"


def test_pages_series_rejects_type_confused_days_object(db_init):
    """Without the P2-15 int-clamp, this object's __gt__ defeats the naive
    `days > 0` gate and its __str__ defeats the `total_pages IS NOT NULL`
    guard via `OR (1=1)`, leaking a NULL-counter reading the function's own
    contract says must never be plotted. int(_EvilDays()) has no __int__, so
    the fixed code must reject it with a controlled TypeError before any SQL
    runs -- proving the clamp, not just luck, is what blocks the injection."""
    db = db_init
    db.store_printer_reading("prn-sn-A", _reading(total_pages=None))
    with pytest.raises(TypeError):
        db.get_printers_pages_series(days=_EvilDays())  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Part 2 (P0-7 addendum): the cutoff comparison itself must use a bound,
# format-matched parameter (db._cutoff_iso), not raw `datetime('now', ...)`
# SQL text compared against a T-separated stored value.
# --------------------------------------------------------------------------- #


def test_pages_series_excludes_reading_genuinely_past_the_window_same_day(db_init):
    db = db_init
    db.store_printer_reading(
        "prn-sn-A", _reading(total_pages=100), received_at=_same_day_but_expired(days=1)
    )
    out = db.get_printers_pages_series(days=1)
    assert out == []  # the only reading is genuinely outside the 1-day window


def test_printer_summary_excludes_job_genuinely_past_the_window_same_day(db_init):
    db = db_init
    db.store_print_jobs(
        "dev-1",
        [{"job_id": 1, "ts": _same_day_but_expired(days=1), "printer": "HP", "pages": 5}],
    )
    out = db.get_printer_print_summary(days=1)
    assert out == []  # the only job is genuinely outside the 1-day window


def test_device_print_excludes_job_genuinely_past_the_window_same_day(db_init):
    db = db_init
    db.store_print_jobs(
        "dev-1",
        [{"job_id": 1, "ts": _same_day_but_expired(days=1), "printer": "HP", "pages": 5}],
    )
    out = db.get_device_print("dev-1", days=1)
    assert out["total_pages"] == 0
    assert out["total_jobs"] == 0
    assert out["printers"] == []
    assert out["daily"] == []
    assert out["recent"] == []


def test_fleet_print_excludes_job_genuinely_past_the_window_same_day(db_init):
    db = db_init
    db.store_print_jobs(
        "dev-1",
        [{"job_id": 1, "ts": _same_day_but_expired(days=1), "printer": "HP", "pages": 5}],
    )
    out = db.get_fleet_print(days=1)
    assert out["total_pages"] == 0
    assert out["total_jobs"] == 0
    assert out["printer_count"] == 0  # pts_f (device/printer rows) also excludes it
    assert out["devices"] == []


def test_print_analytics_boundary_excludes_from_current_but_includes_in_prev(db_init):
    """2 of get_print_analytics' 6 flagged sites: the legacy days-window
    (ts_f/pts_f) AND the period-over-period prev_row cutoff both used to build
    `datetime('now', '-N days')` as raw SQL text. A job ~25h old is genuinely
    in the PREVIOUS 1-day period, not the current one -- proving both are now
    correctly bucketed by the same bound-parameter fix."""
    db = db_init
    db.store_print_jobs(
        "dev-1",
        [{"job_id": 1, "ts": _same_day_but_expired(days=1), "printer": "HP", "pages": 5}],
    )
    out = db.get_print_analytics(days=1)
    assert out["total_pages"] == 0  # NOT in the current 1-day window
    assert out["daily"] == []
    assert out["printers"] == []
    assert out["departments"] == []  # pts_f path
    assert out["prev_total_pages"] == 5  # but correctly counted in the previous period
