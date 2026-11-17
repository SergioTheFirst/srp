"""Публичный стенд (SRP_DEMO=1): вердикты и их причины видны, коэффициенты методики -- нет.

Веса каналов полноты, априорные и веса байесовского ранжирования (и вероятности
классов: у класса без улик она равна априорной), баллы факторов базовых скоров, осей
и улик здоровья, веса доменов доверия и коды правил ступени -- это константы
калибровки (спека витрины 2026-11-04, §8.0). Стенд открыт любому посетителю,
поэтому API отдаёт оценку без них, а шаблоны их не печатают. Сам вердикт -- ступень,
индекс, координаты, срок, причины словами -- остаётся. Обычный сервер не трогается.

for_public -- общий выход API для недоверенного зрителя: на стенде -- без коэффициентов,
из интернета (порт прокси, server/internet_view.py) -- ещё и без ПДн; из сети -- как есть.
"""

from __future__ import annotations

import re
from typing import Any

from server import limits

_NUMBERS = frozenset({"weight", "delta"})
# коды правил ступени (rung/held) и байесовские вероятности, из которых видна априорная
_DROP = frozenset({"rung_source", "held_source", "probability", "overall"})
_WEIGHT_NOTE = re.compile(r"\s*\(вес [0-9.,]+\)")
_PRIOR = "Базовый риск класса"


def strip_weight(label: str) -> str:
    """«источник данных активен (вес 0.35)» -> «источник данных активен»."""
    return _WEIGHT_NOTE.sub("", label)


def _is_prior(item: Any) -> bool:
    return isinstance(item, dict) and str(item.get("label", "")).startswith(_PRIOR)


def redact(obj: Any, _domain: bool = False) -> Any:
    """Глубокая копия без коэффициентов методики; вход не меняется.

    Числа ``weight``/``delta`` снимаются с пунктов-объяснений (у них есть ``label``) и
    с доменов доверия; строки ``(вес N)`` вырезаются из подписей; пункт байесовского
    априорного риска, вероятности классов и коды правил ступени убираются целиком.
    """
    if isinstance(obj, list):
        return [redact(item) for item in obj if not _is_prior(item)]
    if not isinstance(obj, dict):
        return obj
    drop = _NUMBERS if ("label" in obj or _domain) else frozenset()
    out: dict = {}
    for key, value in obj.items():
        if key in drop or key in _DROP:
            continue
        if key == "label" and isinstance(value, str):
            out[key] = strip_weight(value)
        elif key == "domains" and isinstance(value, dict):
            out[key] = {name: redact(dom, _domain=True) for name, dom in value.items()}
        else:
            out[key] = redact(value)
    return out


def for_public(obj: Any) -> Any:
    """Ответ API: на стенде -- без коэффициентов, из интернета -- без ПДн, из сети -- как есть."""
    from server import internet_view  # внутри: этот модуль -- лист, шлюз тянет db и users

    if limits.DEMO_MODE:
        obj = redact(obj)
    return internet_view.redact(obj) if internet_view.is_internet() else obj


def public_label(label: str) -> str:
    """Подпись в шаблоне: на стенде -- без «(вес N)», на обычном сервере -- как есть."""
    return strip_weight(label) if limits.DEMO_MODE else label
