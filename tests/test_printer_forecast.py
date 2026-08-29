"""B3 -- прогноз тонера (ETA) + разбивка печати цвет/ч-б + статус лотка.

``supply_eta_days`` -- чистая Theil-Sen математика (server/printers/forecast.py).
Остальное -- сквозной SSR-рендер /printers и /printers/{id} через ``client``
(премисса плана проверена руками: ``printer_readings.detail`` обнуляется глубже
``_PRINTER_DETAIL_KEEP`` строк (Ш5, «минимальный рост БД») -- истории процента
расходника по факту НЕ было; добавлена аддитивная колонка ``supplies_pct``,
удержание как у соседних скалярных колонок (total_pages и т.п.), НЕ как у
``detail``; см. ``test_supplies_pct_history_survives_detail_keep_boundary``).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# 3.1 -- supply_eta_days: чистая математика, без БД/HTTP
# --------------------------------------------------------------------------- #
def _pts(day_pct_pairs):
    """[(день, процент), ...] -> [(эпоха_секунды, процент), ...]."""
    return [(d * 86400.0, p) for d, p in day_pct_pairs]


def test_linear_decline_100_to_50_over_10_days_gives_about_10():
    from server.printers.forecast import supply_eta_days

    points = _pts([(0, 100), (2, 90), (4, 80), (6, 70), (8, 60), (10, 50)])
    assert supply_eta_days(points) == 10


def test_flat_series_returns_none():
    from server.printers.forecast import supply_eta_days

    points = _pts([(0, 50), (2, 50), (4, 50), (6, 50), (8, 50)])
    assert supply_eta_days(points) is None


def test_rising_series_returns_none():
    from server.printers.forecast import supply_eta_days

    points = _pts([(0, 20), (2, 30), (4, 40), (6, 50), (8, 60)])
    assert supply_eta_days(points) is None


def test_fewer_than_5_points_returns_none():
    from server.printers.forecast import supply_eta_days

    assert supply_eta_days(_pts([(0, 100), (10, 50)])) is None


def test_5_points_under_3_days_span_returns_none():
    from server.printers.forecast import supply_eta_days

    # 5 точек по 1ч -- реальный наклон, но окно короче 3 суток.
    points = [(i * 3600.0, 100 - i) for i in range(5)]
    assert supply_eta_days(points) is None


def test_eta_capped_at_365_days():
    from server.printers.forecast import supply_eta_days

    points = _pts([(0, 100), (100, 99.9), (200, 99.8), (300, 99.7), (400, 99.6)])
    assert supply_eta_days(points) == 365


# --------------------------------------------------------------------------- #
# review B3 HIGH -- a cartridge replacement/refill (a rise mid-series) must
# reset the forecast: the pre-replacement decline is a DIFFERENT cartridge's
# history and must never blend into the current one's slope.
# --------------------------------------------------------------------------- #
def test_eta_resets_after_cartridge_replacement_too_few_fresh_points():
    from server.printers.forecast import supply_eta_days

    # 10 readings declining 100% -> 10% over 18 days, then a refill (rise back
    # to 100%) with only 2 fresh readings -- not enough to forecast on its own.
    points = _pts(
        [(d, p) for d, p in zip(range(0, 20, 2), [100, 92, 84, 76, 68, 60, 52, 44, 28, 10])]
        + [(19, 100), (20, 98)]
    )
    assert supply_eta_days(points) is None


def test_eta_after_cartridge_replacement_uses_only_fresh_segment():
    from server.printers.forecast import supply_eta_days

    # Pre-replacement decline (days 0-8) must be fully discarded once the
    # refill (rise to 100 at day 10) happens; the post-replacement segment
    # (days 10-20) is the exact linear 100->50/10-day shape asserted == 10
    # elsewhere in this file, so a leaking pre-segment would change the answer.
    points = _pts(
        [(0, 50), (2, 40), (4, 30), (6, 20), (8, 10)]
        + [(10, 100), (12, 90), (14, 80), (16, 70), (18, 60), (20, 50)]
    )
    assert supply_eta_days(points) == 10


# --------------------------------------------------------------------------- #
# review B3 HIGH -- Theil-Sen is O(n^2); an unauthenticated /printers list
# feeding years of undownsampled history into it is a DoS. supply_eta_days
# must cap the point count BEFORE the O(n^2) pass, not just run faster on it.
# --------------------------------------------------------------------------- #
def test_supply_eta_days_caps_points_before_on2_slope(monkeypatch):
    import server.printers.forecast as forecast

    seen_lengths = []
    real_slope = forecast.theil_sen_slope

    def spy(points):
        seen_lengths.append(len(points))
        return real_slope(points)

    monkeypatch.setattr(forecast, "theil_sen_slope", spy)
    points = _pts([(i * 0.1, max(0.0, 100 - i * 0.01)) for i in range(3000)])
    forecast.supply_eta_days(points)
    assert seen_lengths == [forecast._MAX_POINTS]


# --------------------------------------------------------------------------- #
# review B3 LOW -- supplies_pct is a device-controlled JSON column: valid JSON
# but a malformed ITEM shape (scalar / dict / 1-element list instead of a
# [name, percent] pair) must be skipped, not raise and 500 the page.
# --------------------------------------------------------------------------- #
def test_eta_by_supply_skips_malformed_item_shapes():
    from server.printers.forecast import eta_by_supply

    rows = [
        {"received_at": "2026-01-01T00:00:00+00:00", "supplies": ["scalar-not-a-pair"]},
        {
            "received_at": "2026-01-02T00:00:00+00:00",
            "supplies": [{"name": "Black", "percent": 10}],
        },
        {"received_at": "2026-01-03T00:00:00+00:00", "supplies": [["Black"]]},
        {"received_at": "2026-01-04T00:00:00+00:00", "supplies": [42]},
    ]
    assert eta_by_supply(rows) == {}  # nothing raised, nothing usable extracted


# --------------------------------------------------------------------------- #
# Премисса: история % расходника переживает обнуление detail (Ш5)
# --------------------------------------------------------------------------- #
def test_supplies_pct_history_survives_detail_keep_boundary(tmp_path):
    from server import db

    db.init_db(tmp_path / "t.db", retain_printer_readings=2000)
    n = db._PRINTER_DETAIL_KEEP + 3
    for i in range(n):
        db.store_printer_reading(
            "prn-hist",
            {
                "ip": "10.0.0.5",
                "status": "idle",
                "total_pages": 100 + i,
                "supplies": [
                    {
                        "name": "Black",
                        "type": "toner",
                        "class_": "consumed",
                        "level": 90 - i,
                        "max": 100,
                        "percent": 90 - i,
                        "unit": 4,
                    }
                ],
                "trays": [],
                "errors": [],
            },
        )
    with sqlite3.connect(db._db_path) as conn:
        rows = conn.execute(
            "SELECT detail, supplies_pct FROM printer_readings ORDER BY id ASC"
        ).fetchall()
    assert len(rows) == n
    assert rows[0][0] is None  # detail обнулён за пределами keep-N (Ш5, не тронуто)
    assert all(r[1] is not None for r in rows)  # supplies_pct жив на КАЖДОЙ строке


# --------------------------------------------------------------------------- #
# review B3 HIGH -- supplies_pct rides EVERY printer_readings row (unlike
# detail); an unbounded SNMP-sourced supply list/name is unbounded DB growth
# from one hostile/buggy printer ([[retention-key-cardinality-unbounded]]).
# --------------------------------------------------------------------------- #
def test_consumed_supply_points_caps_row_count_and_name_length():
    import json

    from server.db import _consumed_supply_points

    supplies = [{"name": "X" * 500, "class_": "consumed", "percent": 50} for _ in range(40)]
    encoded = _consumed_supply_points(supplies)
    pts = json.loads(encoded)
    assert len(pts) <= 16
    assert all(len(p[0]) <= 48 for p in pts)


# --------------------------------------------------------------------------- #
# review B1-B5 final MEDIUM -- an unauthenticated, uncached /printers view
# fetched+json.loads'd up to _retain_prn (2000) rows PER printer here, then
# fed all of it into eta_by_supply's O(n^2) Theil-Sen per supply. supply_eta_
# days already trims each supply's own series to its last 200 points AFTER a
# last-rise scan, so the raw SQL fetch never needed more than a bounded recent
# window either -- this pins that the fetch itself is now capped.
# --------------------------------------------------------------------------- #
def test_get_printers_supply_history_caps_rows_per_printer(tmp_path):
    from server import db

    db.init_db(tmp_path / "t-supplyhist.db", retain_printer_readings=5000)
    now = datetime.now(timezone.utc)
    total_rows = db._SUPPLY_HISTORY_ROWS_PER_PRINTER_MAX + 100
    for i in range(total_rows):
        ts = (now - timedelta(minutes=total_rows - i)).isoformat()
        db.store_printer_reading(
            "prn-manyrows",
            {
                "ip": "10.0.0.9",
                "status": "idle",
                "supplies": [{"name": "Black", "class_": "consumed", "percent": i % 100}],
                "trays": [],
                "errors": [],
            },
            received_at=ts,
        )
    history = db.get_printers_supply_history()
    assert len(history["prn-manyrows"]) <= db._SUPPLY_HISTORY_ROWS_PER_PRINTER_MAX
    # capped to the MOST RECENT rows, not an arbitrary prefix
    assert history["prn-manyrows"][-1]["received_at"] == ts


# --------------------------------------------------------------------------- #
# 3.2 + 3.4 -- сквозной рендер printer_detail.html / printers.html
# --------------------------------------------------------------------------- #
def _seed(db, pid="prn-sn-A", **kw):
    reading = {
        "ip": "192.168.1.5",
        "status": "idle",
        "hostname": "PRN-A",
        "total_pages": 100,
        "supplies": [],
        "trays": [],
        "errors": [],
    }
    reading.update(kw)
    db.store_printer_reading(pid, reading)


def _seed_history(db, pid, points, **extra):
    """points = [(days_ago, percent), ...] для одного расходника "Black Toner"."""
    now = datetime.now(timezone.utc)
    for i, (days_ago, pct) in enumerate(points):
        ts = (now - timedelta(days=days_ago)).isoformat()
        reading = {
            "ip": "10.0.0.9",
            "status": "idle",
            "total_pages": 1000 + i,
            "supplies": [
                {
                    "name": "Black Toner",
                    "type": "toner",
                    "class_": "consumed",
                    "level": pct,
                    "max": 100,
                    "percent": pct,
                    "unit": 4,
                }
            ],
            "trays": [],
            "errors": [],
        }
        reading.update(extra)
        db.store_printer_reading(pid, reading, received_at=ts)


def test_printer_detail_shows_eta_badge_warn_band(client):
    from server import db

    # линейно -5%/сут, 6 точек за 10 сут -> ETA=10 (>7, <=30 -> .warn)
    _seed_history(db, "prn-sn-E1", [(10, 100), (8, 90), (6, 80), (4, 70), (2, 60), (0, 50)])
    r = client.get("/printers/prn-sn-E1")
    assert r.status_code == 200
    assert "≈10 дн" in r.text
    assert 'class="eta warn"' in r.text
    assert "Прогноз по скорости расхода" in r.text


def test_printer_detail_eta_band_bad_at_boundary(client):
    from server import db

    # линейно -5%/сут, 5 точек за 4 сут, сейчас 35% -> ETA=7 (<=7 -> .bad)
    _seed_history(db, "prn-sn-E2", [(4, 55), (3, 50), (2, 45), (1, 40), (0, 35)])
    r = client.get("/printers/prn-sn-E2")
    assert "≈7 дн" in r.text
    assert 'class="eta bad"' in r.text


def test_printer_detail_eta_band_muted_when_far_out(client):
    from server import db

    # линейно -0.5%/сут (целые проценты, как реальный Supply.percent), 6 точек
    # за 20 сут, сейчас 90% -> ETA=180 (>30 -> .muted)
    _seed_history(db, "prn-sn-E3", [(20, 100), (16, 98), (12, 96), (8, 94), (4, 92), (0, 90)])
    r = client.get("/printers/prn-sn-E3")
    assert "≈180 дн" in r.text
    assert 'class="eta muted"' in r.text


def test_printer_detail_no_badge_when_insufficient_history(client):
    from server import db

    _seed_history(db, "prn-sn-E4", [(0, 42)])  # один снимок -- < 5 точек
    r = client.get("/printers/prn-sn-E4")
    assert r.status_code == 200
    assert "≈" not in r.text  # None -> ничего не рендерится


def test_printers_list_shows_worst_supply_eta_badge(client):
    from server import db

    _seed_history(db, "prn-sn-L1", [(4, 55), (3, 50), (2, 45), (1, 40), (0, 35)])
    r = client.get("/printers")
    assert r.status_code == 200
    assert "≈7 дн" in r.text


def test_printers_list_supply_header_explains_pct_and_eta_may_differ(client):
    """MEDIUM (review B1-B5 final): the «Расходник» cell pairs
    low_supply_pct (lowest CURRENT %, any cartridge) with worst_supply_eta_days
    (fastest TREND ETA, possibly a DIFFERENT cartridge) -- neither the column
    header nor the ETA tooltip said the two numbers could refer to different
    consumables (printer_detail.html gives each supply its OWN eta next to its
    OWN percent -- this ambiguity is /printers-list-only)."""
    from server import db

    _seed(db)
    r = client.get("/printers")
    assert r.status_code == 200
    assert "могут относиться к разным расходникам" in r.text


# --------------------------------------------------------------------------- #
# review B1-B5 final MEDIUM -- printer_card looks the ETA up by the FULL
# untruncated supply name (from the latest `detail` blob), but the write side
# (db._consumed_supply_points) truncates the key to db._SUPPLY_NAME_CAP=48
# chars before it ever reaches history -- any supply name over the cap
# (routine on Lexmark/Brother/Ricoh) silently never matched.
# --------------------------------------------------------------------------- #
def test_printer_card_shows_eta_badge_for_supply_name_over_48_chars(client):
    from server import db

    long_name = "Cyan Ultra High Yield Return Program Toner Cartridge"  # 54 chars
    assert len(long_name) > db._SUPPLY_NAME_CAP
    now = datetime.now(timezone.utc)
    for i, (days_ago, pct) in enumerate([(4, 55), (3, 50), (2, 45), (1, 40), (0, 35)]):
        ts = (now - timedelta(days=days_ago)).isoformat()
        db.store_printer_reading(
            "prn-longname",
            {
                "ip": "10.0.0.9",
                "status": "idle",
                "total_pages": 1000 + i,
                "supplies": [
                    {
                        "name": long_name,
                        "type": "toner",
                        "class_": "consumed",
                        "level": pct,
                        "max": 100,
                        "percent": pct,
                        "unit": 4,
                    }
                ],
                "trays": [],
                "errors": [],
            },
            received_at=ts,
        )
    r = client.get("/printers/prn-longname")
    assert r.status_code == 200
    assert "≈7 дн" in r.text


# --------------------------------------------------------------------------- #
# review B3 MEDIUM -- per-row supply history only ever feeds the ETA computed
# server-side; neither the chart JS nor the JSON API consumer reads it, so it
# must not ride along x500 rows into the embedded page DOM or the public API.
# --------------------------------------------------------------------------- #
def test_printer_detail_page_does_not_embed_supplies_history_blob(client):
    from server import db

    _seed_history(db, "prn-sn-M1", [(10, 100), (8, 90), (6, 80), (4, 70), (2, 60), (0, 50)])
    r = client.get("/printers/prn-sn-M1")
    assert r.status_code == 200
    assert '"supplies"' not in r.text


def test_printer_api_detail_does_not_leak_supplies_history(client):
    from server import db

    _seed_history(db, "prn-sn-M2", [(10, 100), (8, 90), (6, 80), (4, 70), (2, 60), (0, 50)])
    r = client.get("/api/v1/printers/prn-sn-M2")
    assert r.status_code == 200
    body = r.json()
    assert body["series"]
    assert all("supplies" not in row for row in body["series"])


# --------------------------------------------------------------------------- #
# 3.3 -- график: линии «цвет» / «ч-б»
# --------------------------------------------------------------------------- #
def test_printer_detail_chart_config_has_color_and_mono_lines(client):
    from server import db

    _seed(db, pid="prn-sn-CH")
    r = client.get("/printers/prn-sn-CH")
    assert "цвет" in r.text
    assert "ч-б" in r.text


# --------------------------------------------------------------------------- #
# 3.4 -- бейдж статуса лотка (prtInputStatus)
# --------------------------------------------------------------------------- #
def test_tray_abnormal_status_shows_bad_badge(client):
    from server import db

    _seed(
        db,
        pid="prn-sn-T1",
        trays=[{"name": "Tray 1", "media": "A4", "level": 200, "max": 500, "status": 16}],
    )
    r = client.get("/printers/prn-sn-T1")
    assert r.status_code == 200
    assert "chip bad" in r.text
    assert "критическая ошибка" in r.text


def test_tray_normal_status_shows_no_badge(client):
    from server import db

    _seed(
        db,
        pid="prn-sn-T2",
        trays=[
            {"name": "Tray 1", "media": "A4", "level": 200, "max": 500, "status": 0},
            {"name": "Tray 2", "media": "A4", "level": 100, "max": 500, "status": 6},
        ],
    )
    r = client.get("/printers/prn-sn-T2")
    assert "Tray 1" in r.text and "Tray 2" in r.text
    assert "критическая ошибка" not in r.text
    assert "неисправен" not in r.text
    assert "офлайн" not in r.text


def test_tray_unknown_status_code_shown_as_neutral_text_fail_open(client):
    from server import db

    _seed(
        db,
        pid="prn-sn-T3",
        trays=[{"name": "Tray 1", "media": "A4", "level": 200, "max": 500, "status": 9999}],
    )
    r = client.get("/printers/prn-sn-T3")
    assert "9999" in r.text  # неизвестный код виден текстом, а не спрятан
    assert "chip na" in r.text


def test_tray_without_status_shows_no_badge(client):
    from server import db

    _seed(
        db,
        pid="prn-sn-T4",
        trays=[{"name": "Tray 1", "media": "A4", "level": 200, "max": 500, "status": None}],
    )
    r = client.get("/printers/prn-sn-T4")
    assert r.status_code == 200
    assert "chip na" not in r.text


def test_tray_status_badge_fail_open_for_unrecognized_availability_bits():
    # review B3 LOW: availability bits (status & 0x07) == 7 are reserved/
    # undefined by RFC 3805; with no alert flags set this fell through to
    # "normal, no badge" instead of showing fail-open like an out-of-range code.
    from server.web.dashboard import tray_status_badge

    assert tray_status_badge(7) == ("na", "7")
    assert tray_status_badge(71) == ("na", "71")  # 7 + transitioning(64), no alert flags
    # known-normal codes: unaffected, still no badge.
    assert tray_status_badge(0) is None
    assert tray_status_badge(6) is None
    # a real alert combined with unrecognized availability bits still surfaces
    # as the alert, not downgraded to neutral.
    assert tray_status_badge(7 | 0x10) == ("bad", "критическая ошибка")


def test_tray_status_badge_has_tooltip(client):
    from server import db

    _seed(
        db,
        pid="prn-sn-T5",
        trays=[{"name": "Tray 1", "media": "A4", "level": 200, "max": 500, "status": 16}],
    )
    r = client.get("/printers/prn-sn-T5")
    assert r.status_code == 200
    assert 'title="Код статуса лотка (prtInputStatus, SNMP)"' in r.text
