"""Пины редизайна карточки устройства (2026-07-15): иерархия для инженера.

Сеялка — тот же минимальный паттерн, что в tests/test_device_hero.py::_seed
(пишем напрямую в хранилище, минуя скоринг-пайплайн).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from server import db

pytestmark = pytest.mark.integration


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed(device_id: str, hostname: str, risk: dict) -> None:
    # day1_factors обязателен (страница чейнит его без guard — см. docstring
    # test_device_hero._seed); classes/domains добавлены защитно.
    risk = {
        "day1_factors": {"performance": [], "reliability": [], "wear": [], "risk_exposure": []},
        "classes": [],
        "domains": {},
        **risk,
    }
    db.touch_device(device_id, _iso_now(), "0.1.0", hostname=hostname)
    db.store_scores(device_id, _iso_now(), {"risk": risk})


def _axis(value, confidence="high", reason="", factors=None):
    return {
        "value": value,
        "confidence": confidence,
        "reason": reason,
        "factors": factors or [],
        "missing_evidence": [],
    }


# --------------------------------------------------------------------------- #
# T1: правило видимости осей
# --------------------------------------------------------------------------- #
def test_axis_over_threshold_visible_before_details(client) -> None:
    _seed(
        "card-1",
        "CARD-1",
        {"score100": {"storage_risk": _axis(55.0, reason="ошибки чтения растут")}},
    )
    body = client.get("/device/card-1").text
    axis = body.find("Здоровье диска (SMART)")
    details = body.find('id="device-diagnostics"')
    assert axis != -1 and details != -1
    assert axis < details, "плохая ось (55) должна быть видна сразу, не в раскрывашке"


def test_axis_healthy_hidden_inside_details(client) -> None:
    _seed("card-2", "CARD-2", {"score100": {"storage_risk": _axis(5.0)}})
    body = client.get("/device/card-2").text
    axis = body.find("Здоровье диска (SMART)")
    details = body.find('id="device-diagnostics"')
    assert axis != -1 and details != -1
    assert details < axis, "здоровая ось (5) должна лежать внутри раскрывашки"


def test_axis_unknown_value_hidden_inside_details(client) -> None:
    _seed("card-3", "CARD-3", {"score100": {"network_risk": _axis(None, confidence=None)}})
    body = client.get("/device/card-3").text
    axis = body.find("Здоровье сети")
    details = body.find('id="device-diagnostics"')
    assert details != -1 and axis != -1 and details < axis


def test_axis_confidence_is_labelled(client) -> None:
    _seed("card-4", "CARD-4", {"score100": {"storage_risk": _axis(55.0)}})
    body = client.get("/device/card-4").text
    assert "уверенность: высокая" in body
    assert 'class="axis-conf"' in body


def test_all_clear_line_when_every_axis_healthy(client) -> None:
    _seed(
        "card-5", "CARD-5", {"score100": {"storage_risk": _axis(3.0), "disk_fill_risk": _axis(8.0)}}
    )
    body = client.get("/device/card-5").text
    assert "По рассчитанным проверкам замечаний нет" in body


def test_no_axes_never_claims_all_clear(client) -> None:
    _seed("card-6", "CARD-6", {})  # score100 отсутствует (стар. устройство)
    body = client.get("/device/card-6").text
    assert "Оси диагностики ещё не рассчитаны" in body
    # Narrow pin (not a bare "замечаний нет" substring check): the page's
    # unrelated Day-1 factor drilldowns ("Диагностика — что повлияло…", out of
    # scope for T1) legitimately render their own "Замечаний нет." whenever a
    # factor group is empty -- which this seed's day1_factors always is. Only
    # the exact all-clear sentence this section can produce must be absent.
    assert "По рассчитанным проверкам замечаний нет" not in body


def test_trajectory_axis_renamed_no_english_calque(client) -> None:
    _seed("card-7", "CARD-7", {"score100": {"trajectory_risk": _axis(40.0)}})
    body = client.get("/device/card-7").text
    assert "Риск по трендам" in body
    assert "Риск траектории" not in body


def test_mixed_healthy_and_unknown_axes_not_shown_as_all_clear(client) -> None:
    # Review finding: ns_ax.rest used to lump "healthy" (value < 25) and "unknown"
    # (value is None) axes together with no way to tell them apart, so this exact
    # combination (nothing in attention, but one axis unresolved) rendered a false
    # "all clear" -- violates the "UNKNOWN over false confidence" invariant (CLAUDE.md §5).
    _seed(
        "card-8",
        "CARD-8",
        {"score100": {"storage_risk": _axis(5.0), "network_risk": _axis(None, confidence=None)}},
    )
    body = client.get("/device/card-8").text
    assert "По рассчитанным проверкам замечаний нет" not in body
    assert "Без замечаний: 1" in body
    assert "нет данных для оценки: 1" in body
