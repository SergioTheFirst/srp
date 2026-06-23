"""Print query layer: PrintFilter + parameterized WHERE builder (printview Phase 2).

Pure unit tests -- the builder is the shared, DRY core every print view reuses
(series / summary / records / export). The security-critical invariant: filter
VALUES are always bound parameters, never interpolated into SQL text.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_empty_filter_yields_empty_where() -> None:
    from server.db import PrintFilter, _print_where

    where, params = _print_where(PrintFilter())
    assert where == ""
    assert params == []


def test_device_printer_ip_become_bound_params() -> None:
    from server.db import PrintFilter, _print_where

    where, params = _print_where(PrintFilter(device="dev-1", printer="HP", ip="192.168.1.5"))
    assert "p.device_id = ?" in where
    assert "p.printer = ?" in where
    assert "m.ip = ?" in where
    assert params == ["dev-1", "HP", "192.168.1.5"]


def test_dates_become_two_bound_cutoffs() -> None:
    from server.db import PrintFilter, _print_where

    where, params = _print_where(PrintFilter(date_from="2026-06-01", date_to="2026-06-30"))
    assert "p.ts >= ?" in where
    assert "p.ts < ?" in where
    assert len(params) == 2
    # raw user date must not leak into the SQL text -- only placeholders.
    assert "2026" not in where


def test_injection_attempt_stays_a_bound_param() -> None:
    from server.db import PrintFilter, _print_where

    where, params = _print_where(PrintFilter(device="x'; DROP TABLE print_jobs;--", printer="p"))
    assert "DROP TABLE" not in where
    assert where.count("?") == 2
    assert params[0] == "x'; DROP TABLE print_jobs;--"


def test_malformed_date_injection_is_dropped() -> None:
    from server.db import PrintFilter, _print_where

    # A malformed/injection date is rejected by strptime -> no clause, no param,
    # so the payload never reaches SQL text (defense-in-depth alongside binding).
    where, params = _print_where(PrintFilter(date_from="2026-06-01'; DROP TABLE print_jobs;--"))
    assert "DROP TABLE" not in where
    assert where == ""
    assert params == []


def test_normalize_dates_swaps_reversed_range() -> None:
    from server.db import _normalize_dates

    assert _normalize_dates("2026-06-30", "2026-06-01") == ("2026-06-01", "2026-06-30")
    assert _normalize_dates("2026-06-01", "2026-06-30") == ("2026-06-01", "2026-06-30")
    assert _normalize_dates(None, None) == (None, None)
    assert _normalize_dates("2026-06-01", None) == ("2026-06-01", None)


def test_date_cutoff_rejects_malformed() -> None:
    from server.db import _date_cutoff_utc

    assert _date_cutoff_utc("not-a-date", end=False) is None
    assert _date_cutoff_utc("", end=False) is None
    assert _date_cutoff_utc(None, end=False) is None
    assert _date_cutoff_utc("2026-13-99", end=False) is None


def test_date_cutoff_end_is_next_day_midnight() -> None:
    from server.db import _date_cutoff_utc

    lo = _date_cutoff_utc("2026-06-01", end=False)
    hi = _date_cutoff_utc("2026-06-01", end=True)
    assert lo is not None and hi is not None
    assert hi > lo  # inclusive date_to -> start of the next local day
