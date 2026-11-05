"""Калибровка вердикта здоровья, T7: формулировки карточки, парка и документации.

Спека docs/superpowers/specs/2026-10-09-health-calibration-design.md:
D5 -- срок реакции вместо «окна до ухудшения»; D7 -- hero показывает итоговое
действие вердикта; D10 -- байесовские классы уровнем, без процента; D11 --
подсказки device.html. Блобы health сеются напрямую, как в
tests/test_device_hero.py (T6 может быть ещё не слита).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from server import db

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parents[1]
_CALM = "наблюдать — повреждений не выявлено, действий не требуется"


def _seed(device_id: str, risk: dict) -> None:
    # day1_factors is mandatory: device.html chains it without a guard
    # (see tests/test_device_hero.py::_seed).
    ts = datetime.now(timezone.utc).isoformat()
    blank = {"performance": [], "reliability": [], "wear": [], "risk_exposure": []}
    db.touch_device(device_id, ts, "0.1.0", hostname=device_id.upper())
    db.store_scores(device_id, ts, {"risk": {"day1_factors": blank, **risk}})


def _health(**over: Any) -> dict:
    base = {
        "damage": {"value": 60.0, "band": "bad", "confidence": "high", "evidence": []},
        "resilience": {"value": 50.0, "band": "watch", "confidence": "high", "evidence": []},
        "observability": {"value": 90.0, "band": "good", "confidence": "high", "evidence": []},
        "blind_spots": [],
        "state": "h4",
        "state_label": "предотказ",
        "state_evidence": [],
        "index": 30.0,
        "band": "bad",
        "confidence": "high",
        "dominant": "storage",
        "dominant_label": "накопитель",
        "horizon_days": 7,
        "horizon_kind": "state",
        "horizon_reason": "",
        "action": "снять образ данных, планировать замену накопителя",
        "factors": [],
        "missing_evidence": [],
        "delta_7d": None,
    }
    return {**base, **over}


def _without(blob: dict, key: str) -> dict:
    return {k: v for k, v in blob.items() if k != key}


def _hero(client, device_id: str, health: dict) -> str:
    _seed(device_id, {"health": health})
    body = client.get(f"/device/{device_id}").text
    return body[body.index('id="device-hero"') : body.index('id="attention-label"')]


# --------------------------------------------------------------------------- #
# D5: срок реакции / прогноз по тренду вместо «окна до ухудшения»
# --------------------------------------------------------------------------- #
def test_hero_state_horizon_says_reaction_window(client) -> None:
    frag = _hero(client, "cu-1", _health(horizon_days=7, horizon_kind="state"))
    assert "срок реакции: ≤7 дней" in frag
    assert 'title="срок реакции назначается по состоянию — это не прогноз отказа"' in frag
    assert "окно до ухудшения" not in frag


def test_hero_eta_horizon_says_forecast(client) -> None:
    reason = (
        "прогноз по тренду (свободное место на системном диске до 5%): предел в пределах 30 дней"
    )
    health = _health(horizon_days=30, horizon_kind="eta", horizon_reason=reason)
    frag = _hero(client, "cu-2", health)
    assert "прогноз по тренду: ≤30 дней" in frag
    assert f'title="{reason}"' in frag
    assert "срок реакции: ≤30 дней" not in frag


def test_hero_without_horizon_says_not_assigned(client) -> None:
    health = _health(state="h1", band="watch", horizon_days=None, horizon_kind=None)
    frag = _hero(client, "cu-3", health)
    assert "срок реакции: не назначен" in frag
    assert "не прогнозируется" not in frag


def test_hero_old_row_without_kind_renders_as_state(client) -> None:
    old_row = _without(_health(horizon_days=90), "horizon_kind")
    assert "срок реакции: ≤90 дней" in _hero(client, "cu-4", old_row)
    # fail-open: a kind this template does not know still renders, worded as 'state'
    future = _health(horizon_days=30, horizon_kind="future-kind")
    assert "срок реакции: ≤30 дней" in _hero(client, "cu-5", future)


# --------------------------------------------------------------------------- #
# D7: hero = итоговое действие вердикта (то же, что Zabbix srp.reason)
# --------------------------------------------------------------------------- #
def test_hero_renders_verdict_action(client) -> None:
    health = _health(
        state="h0",
        state_label="здоров",
        band="good",
        horizon_days=None,
        horizon_kind=None,
        action=_CALM,
    )
    frag = _hero(client, "cu-6", health)
    assert _CALM in frag
    assert "снять образ данных" not in frag  # action_for("storage") must not override


def test_hero_old_row_without_action_falls_back_to_dominant(client) -> None:
    old_row = _without(_health(dominant="network", dominant_label="сеть"), "action")
    assert "проверить линк/кабель/точку доступа" in _hero(client, "cu-7", old_row)


def test_fleet_horizon_titles_are_not_forecast(client) -> None:
    _seed("cu-8", {"health": _health(horizon_days=7, horizon_reason="")})
    body = client.get("/fleet/fragment").text
    assert "прогноз окна до ухудшения" not in body
    assert (
        'title="главный фактор и срок реакции (по состоянию) или прогноз по тренду,'
        ' если он короче"' in body
    )
    assert 'title="срок реакции по состоянию — не прогноз отказа">≤7дн<' in body


# --------------------------------------------------------------------------- #
# D10: байесовские классы -- уровень словом, не «вероятность отказа»
# --------------------------------------------------------------------------- #
def test_bayes_block_shows_level_not_probability(client) -> None:
    classes = [
        {
            "name": "storage",
            "label": "деградация накопителя",
            "probability": 0.62,
            "level": "critical",
            "trust": "trusted",
            "factors": [{"label": "износ SSD", "weight": 1.2}],
        },
        {
            "name": "memory",
            "label": "память",
            "probability": 0.12,
            "level": "someday",
            "trust": None,
            "factors": [],
        },
    ]
    _seed("cu-9", {"classes": classes})
    body = client.get("/device/cu-9").text
    start = body.index("Классы отказа")
    block = body[start : body.index("Диагностика — что повлияло", start)]
    assert "Классы отказа (байесовская оценка риска, не калибрована)" in block
    assert (
        'title="Относительный приоритет по классам; веса не откалиброваны'
        ' — это не вероятность отказа"' in body
    )
    assert ">критический<" in block
    assert ">someday<" in block  # unknown level: raw value, never hidden
    assert 'title="байесовская оценка риска (не калибрована) по классу «память»"' in block
    assert "положительный — повышает оценку риска, отрицательный — понижает" in block
    assert ">62%<" not in block
    assert 'class="riskbar' in block  # the bar stays
    assert "Вероятность отказа" not in body
    assert "вероятности отказа по каждому" not in body


# --------------------------------------------------------------------------- #
# D11: подсказки device.html
# --------------------------------------------------------------------------- #
def test_risk_tooltip_uses_15_and_40(client) -> None:
    axis = {
        "value": 55.0,
        "confidence": "high",
        "reason": "",
        "factors": [],
        "missing_evidence": [],
    }
    _seed("cu-10", {"score100": {"storage_risk": axis}})
    body = client.get("/device/cu-10").text
    assert "Зелёный &lt; 15, оранжевый 15–39, красный ≥ 40" in body  # "<" is autoescaped
    assert "≤ 24" not in body


def _trend(current: float) -> dict:
    return {
        "direction": "worsening",
        "slope_per_day": -0.5,
        "current": current,
        "eta_days": None,
        "target_date": None,
    }


def test_disk_fill_row_shows_fill(client) -> None:
    _seed("cu-11", {"trajectory": {"disk_fill": _trend(3.0), "storage_wear": _trend(40.0)}})
    body = client.get("/device/cu-11").text
    start = body.index(">Заполнение диска</div>")
    row = body[start : body.index("traj-right", start)]
    assert "97.0%" in row  # trend current is FREE space (trends._free_space_pct)
    assert "3.0%" not in row
    assert "width:97.0%" in row  # the bar draws fill too
    assert "(100 − свободное место)" in body
    assert "до порога 5" in body
    assert "плановая информация, не прогноз отказа" in body


def test_trajectory_axis_na_tip_states_the_calibrated_gate(client) -> None:
    """A trend now needs >= 6 readings over >= 24 h (72 h for slow series), not 3 points."""
    _seed("cu-13", {"score100": {"trajectory_risk": {"value": None, "band": "unknown"}}})
    body = client.get("/device/cu-13").text
    assert "нужно ≥ 6 показаний за ≥ 24 ч" in body
    assert "нужно минимум 3" not in body


def test_trend_tooltips_render_single_percent(client) -> None:
    """Jinja string literals are not %-formatted: '%%' rendered verbatim in titles."""
    _seed("cu-12", {"trajectory": {"disk_fill": _trend(30.0), "storage_wear": _trend(40.0)}})
    body = client.get("/device/cu-12").text
    assert "%%" not in body
    assert "до порога 5% свободного места" in body


# --------------------------------------------------------------------------- #
# Документация и CHANGELOG
# --------------------------------------------------------------------------- #
def test_zabbix_docs_say_reaction_window() -> None:
    template = (_ROOT / "docs" / "zabbix" / "srp_template.yaml").read_text(encoding="utf-8")
    export_doc = (_ROOT / "docs" / "zabbix-export.md").read_text(encoding="utf-8")
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    assert "name: 'SRP: срок реакции'" in template
    assert "окно до ухудшения" not in template
    assert "срок реакции, не прогноз отказа" in export_doc
    assert "Окно до ожидаемого ухудшения" not in export_doc
    assert "срок реакции в сутках (по состоянию) или прогноз по тренду" in readme


def test_changelog_has_calibration_lines() -> None:
    path = _ROOT / "CHANGELOG.md"
    if not path.is_file():
        pytest.skip("CHANGELOG ведётся в основном репозитории, в публичное дерево не входит")
    text = path.read_text(encoding="utf-8")
    # the calibration shipped in 1.0.0 (2026-11-05): its lines live in that release section
    start = text.index("\n## [1.0.0]")
    released = text[start : text.index("\n## [", start + 1)]
    assert "Вердикт здоровья откалиброван" in released
    assert "Повторно присланные агентом события" in released
