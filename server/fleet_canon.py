"""Канонический ПК: какая строка devices вытеснена переустановкой (2026-10-24).

Чистый лист без импорта db -- одно правило на все флотские срезы (/, /health, API,
Zabbix). Спека: docs/superpowers/specs/2026-10-24-canonical-devices-design.md.

Строка R вытеснена, если в её группе (org, site, dept, hostname без регистра) есть K,
который появился, когда R уже замолчал (K.first_seen > R.last_seen), и K «состоялся»:
на связи или прожил хотя бы час. Свежесть преемника не нужна -- ночью дубль тоже свёрнут.
Перекрытие жизней = тёзки, видны обе. Строку с самым поздним first_seen ничто не
вытесняет, поэтому группа не пустеет. Возраст -- секунды от СЕРВЕРНЫХ меток;
непарсибельный возраст выводит строку из сравнения (UNKNOWN over false confidence).
"""

from __future__ import annotations

from bisect import bisect_left
from typing import Any, Iterable, Mapping

# ponytail: час -- гард от разового УСТАРЕВШЕГО поддельного конверта при пустом
# ingest_token (молчащий ПК не прячется одним конвертом). Непрерывная подделка
# прячет любой ПК, как и раньше; полное лечение -- задать ingest_token.
ESTABLISHED_AFTER_SEC = 3600


def dupe_key(row: Mapping[str, Any]) -> tuple:
    """Ключ группы тёзок: одно имя В ПРЕДЕЛАХ одного объекта/орг/отдела.
    Разные объекты с одинаковым generic-именем (PC-01, KASSA) -- разные машины."""
    return (
        (row.get("org_code") or "").strip().casefold(),
        (row.get("site_code") or "").strip().casefold(),
        (row.get("dept_code") or "").strip().casefold(),
        (row.get("hostname") or "").strip().casefold(),
    )


def _comparable(row: Mapping[str, Any]) -> bool:
    return (
        bool((row.get("hostname") or "").strip())
        and row.get("first_seen_age_sec") is not None
        and row.get("last_seen_age_sec") is not None
    )


def _established(row: Mapping[str, Any]) -> bool:
    lived = row["first_seen_age_sec"] - row["last_seen_age_sec"]
    return not row.get("stale") or lived >= ESTABLISHED_AFTER_SEC


def _nearest_in_group(group: list) -> dict[str, str]:
    """{R: ближайший преемник} за O(g log g): преемник -- состоявшийся K с наибольшим
    first_seen_age, который всё же меньше R.last_seen_age (родился раньше всех
    после смерти R). O(g^2) здесь -- DoS: тысячи поддельных id с одним именем
    (ревью 2026-10-24)."""
    est = sorted((k["first_seen_age_sec"], k["device_id"]) for k in group if _established(k))
    firsts = [f for f, _ in est]
    out: dict[str, str] = {}
    for r in group:
        i = bisect_left(firsts, r["last_seen_age_sec"]) - 1
        if i >= 0 and est[i][1] == r["device_id"]:
            i -= 1  # себя не считаем (бывает при first_seen > last_seen в кривых данных)
        if i >= 0:
            out[r["device_id"]] = est[i][1]
    return out


def _resolve_chains(nearest: dict[str, str]) -> dict[str, str]:
    """Конец цепочки переустановок R1 -> R2 -> R3 с мемо (O(n)). Порядок по first_seen
    строгий, цикл возможен только на кривых данных -- обрываем его на месте."""
    final: dict[str, str] = {}
    for ghost in nearest:
        path, on_path, cur = [], set(), ghost
        while cur in nearest and cur not in final and cur not in on_path:
            path.append(cur)
            on_path.add(cur)
            cur = nearest[cur]
        end = final.get(cur, cur)
        for node in path:
            final[node] = end
    return {ghost: end for ghost, end in final.items() if ghost != end}


def superseded_by(rows: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """{device_id вытесненной: device_id канонической записи (конец цепочки)}.

    Ряд: device_id, hostname, org/site/dept_code, first_seen_age_sec,
    last_seen_age_sec, stale. Пусто -> вытесненных нет.
    """
    groups: dict[tuple, list] = {}
    for r in rows:
        if _comparable(r):
            groups.setdefault(dupe_key(r), []).append(r)
    nearest: dict[str, str] = {}
    for group in groups.values():
        if len(group) > 1:
            nearest.update(_nearest_in_group(group))
    return _resolve_chains(nearest)
