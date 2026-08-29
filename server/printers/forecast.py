"""B3 -- прогноз расходников принтера: дни до 0% по устойчивому наклону.

Чистая математика (D4, без ML): Theil-Sen медиана попарных наклонов
(``server.analytics.trends.theil_sen_slope``) поверх истории процента
расходника (``db.get_printer_series`` / ``db.get_printers_supply_history``,
колонка ``printer_readings.supplies_pct``). UNKNOWN over false confidence --
мало точек / короткое окно / уровень не падает -> None, никогда не выдуманное
число (CLAUDE.md §5).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from server.analytics.trends import theil_sen_slope

# ≥5 точек за ≥3 суток (план B3 3.1) -- меньше данных не отличить от шума
# 15-минутного опроса.
_MIN_POINTS = 5
_MIN_SPAN_DAYS = 3.0
_MAX_ETA_DAYS = 365
_SECONDS_PER_DAY = 86400.0
# Theil-Sen -- O(n^2) пар; без кепа /printers (auth-less) годами копящаяся
# история одного расходника (default poll 900s -> ~2000 точек / 90-дневное
# окно get_printers_supply_history) даёт секунды на КАЖДЫЙ запрос списка
# (review B3 HIGH). Последние N точек несут и более свежий (более уместный
# для ETA) темп расхода, а не просто быстрее считаются.
_MAX_POINTS = 200


def _epoch(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def supply_eta_days(points: list[tuple[float, float]]) -> Optional[int]:
    """Дни до 0% по устойчивому (Theil-Sen) наклону истории процента расходника.

    ``points`` = [(эпоха_секунды, процент), ...], в любом порядке. None (UNKNOWN),
    когда: точек < 5, история короче 3 суток, или уровень не падает (плоско/
    растёт -- расходник не расходуется предсказуемо, а не "никогда не кончится").
    Результат ограничен 365 днями.
    """
    pts = sorted(points)
    # A rise mid-series means the consumable was replaced/refilled -- the
    # pre-replacement decline is a different cartridge's history and must not
    # blend into this one's slope (review B3 HIGH: stale ETA survived a
    # refill). Keep only the segment at/after the LAST rise.
    for i in range(len(pts) - 1, 0, -1):
        if pts[i][1] > pts[i - 1][1]:
            pts = pts[i:]
            break
    if len(pts) > _MAX_POINTS:
        pts = pts[-_MAX_POINTS:]
    if len(pts) < _MIN_POINTS:
        return None
    span_days = (pts[-1][0] - pts[0][0]) / _SECONDS_PER_DAY
    if span_days < _MIN_SPAN_DAYS:
        return None
    slope = theil_sen_slope(pts)  # % в сутки; None если точки не разнесены во времени
    if slope is None or slope >= 0:
        return None
    pct_now = pts[-1][1]
    eta = round(pct_now / -slope)
    return max(0, min(_MAX_ETA_DAYS, eta))


def eta_by_supply(rows: list[dict[str, Any]]) -> dict[str, Optional[int]]:
    """{имя расходника: ETA в днях или None} из строк истории опроса.

    ``rows`` = [{"received_at": iso, "supplies": [[name, percent], ...] | None}, ...]
    -- форма ``db.get_printer_series`` / ``db.get_printers_supply_history``.
    """
    points_by_name: dict[str, list[tuple[float, float]]] = {}
    for row in rows or []:
        ts = _epoch(row.get("received_at"))
        if ts is None:
            continue
        for item in row.get("supplies") or []:
            # supplies_pct is JSON-valid-but-shape-untrusted (device-controlled
            # history column) -- a scalar/dict/1-element item must not blow up
            # the whole /printers or /printers/{id} page (review B3 LOW).
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            name, pct = item
            if pct is None or not name:
                continue
            points_by_name.setdefault(name, []).append((ts, float(pct)))
    return {name: supply_eta_days(pts) for name, pts in points_by_name.items()}


def worst_eta_days(rows: list[dict[str, Any]]) -> Optional[int]:
    """Наименьший (самый близкий) ETA среди расходников принтера, либо None."""
    etas = [v for v in eta_by_supply(rows).values() if v is not None]
    return min(etas) if etas else None
