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


def test_axis_value_is_not_a_bare_number(client) -> None:
    # Final whole-branch review finding: axis-val rendered a bare "55" with the
    # 0-100 scale explained only via title= -- violates the plan's own Global
    # Constraint ("никаких голых чисел"). Fixed by appending "из 100".
    _seed("card-17", "CARD-17", {"score100": {"storage_risk": _axis(55.0)}})
    body = client.get("/device/card-17").text
    assert "55 из 100" in body


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


# --------------------------------------------------------------------------- #
# T2: старая иерархия внутри раскрывашки, «Риск-экспозиция» переименована
# --------------------------------------------------------------------------- #
def test_risk_exposure_renamed_everywhere(client) -> None:
    _seed("card-8", "CARD-8", {})
    body = client.get("/device/card-8").text
    assert "Риск-экспозиция" not in body
    assert "Суммарный риск сбоя" in body


def test_day1_scorecards_inside_details(client) -> None:
    _seed("card-9", "CARD-9", {})
    body = client.get("/device/card-9").text
    details_open = body.find('id="device-diagnostics"')
    # Not body.find("</details>", ...): the Day-1 factor drilldowns nest their
    # own <details> per group, so the first "</details>" after the drawer opens
    # belongs to an inner group, not the outer drawer. "Недавние события" is the
    # next unconditional section right after the drawer's real close (device.html).
    details_close = body.find("Недавние события", details_open)
    day1 = body.find("Производительность")
    assert details_open != -1 and details_close != -1 and day1 != -1
    assert details_open < day1 < details_close, (
        "старые сводные баллы должны лежать внутри раскрывашки"
    )


def test_coverage_widget_moved_but_string_preserved(client) -> None:
    """«Покрытие источников» запинено test_dashboard_trust.py:41 — строка обязана
    остаться в DOM (внутри раскрывашки)."""
    _seed(
        "card-10",
        "CARD-10",
        {"domains": {"smart": {"state": "trusted", "weight": 1.0}}, "classes": []},
    )
    body = client.get("/device/card-10").text
    details_open = body.find('id="device-diagnostics"')
    # See test_day1_scorecards_inside_details for why not body.find("</details>", ...).
    details_close = body.find("Недавние события", details_open)
    cov = body.find("Покрытие источников")
    assert details_open != -1 and details_close != -1 and cov != -1
    assert details_open < cov < details_close, (
        "должно лежать внутри раскрывашки, не просто после её открытия"
    )


def test_failure_classes_inside_details(client) -> None:
    _seed(
        "card-11",
        "CARD-11",
        {
            "classes": [
                {
                    "label": "деградация накопителя",
                    "trust": "unknown",
                    "level": "low",
                    "probability": 0.1,
                    "factors": [],
                }
            ]
        },
    )
    body = client.get("/device/card-11").text
    details_open = body.find('id="device-diagnostics"')
    # See test_day1_scorecards_inside_details for why not body.find("</details>", ...).
    details_close = body.find("Недавние события", details_open)
    cls = body.find("Классы отказа")
    assert details_open != -1 and details_close != -1 and cls != -1
    assert details_open < cls < details_close, (
        "должно лежать ВНУТРИ раскрывашки, не просто после её открытия"
    )


# --------------------------------------------------------------------------- #
# T3: блок «Компьютер» вверху, «Инвентарь» снесён
# --------------------------------------------------------------------------- #
_INV = {
    "hostname": "CARD-PC",
    "os_caption": "Microsoft Windows 10 Pro",
    "os_version": "10.0.19045",
    "os_build": "19045",
    "cpu_name": "Intel(R) Core(TM) i5-10400",
    "cpu_cores": 6,
    "cpu_logical": 12,
    "total_ram_gb": 16,
    "memory_modules": [{"capacity_gb": 8, "speed_mhz": 2666, "manufacturer": "Kingston"}],
    "disks": [{"model": "Samsung SSD 870 EVO", "media_type": "SSD", "size_gb": 500}],
    "bios_version": "1.5.0",
    "bios_release_date": "2022-09-01",
    "pending_reboot": True,
    "driver_problem_count": 2,
}


def _seed_with_inventory(device_id: str, hostname: str) -> None:
    _seed(device_id, hostname, {})
    db.store_inventory(device_id, _iso_now(), _INV)


def test_specs_block_labelled_and_on_top(client) -> None:
    _seed_with_inventory("card-12", "CARD-12")
    body = client.get("/device/card-12").text
    for label in ("Процессор", "Память", "Диски", "Система"):
        assert label in body, f"подпись «{label}» обязана быть в блоке «Компьютер»"
    assert "Intel(R) Core(TM) i5-10400" in body
    assert "ядер: 6" in body
    assert "Samsung SSD 870 EVO" in body
    # блок «Компьютер» идёт раньше вердикта
    assert body.find("Процессор") < body.find('id="device-hero"')


def test_specs_flags_pending_reboot_and_drivers(client) -> None:
    _seed_with_inventory("card-13", "CARD-13")
    body = client.get("/device/card-13").text
    assert "требуется перезагрузка" in body
    assert "проблемных драйверов: 2" in body


def test_old_inventory_section_gone(client) -> None:
    _seed_with_inventory("card-14", "CARD-14")
    body = client.get("/device/card-14").text
    assert ">Инвентарь<" not in body
    # но содержимое не потеряно: BIOS и период активности живут в раскрывашке блока
    assert "BIOS" in body
    assert "Период активности" in body


def test_specs_fallback_when_no_inventory(client) -> None:
    _seed("card-15", "CARD-15", {})
    body = client.get("/device/card-15").text
    assert "Характеристики ещё не получены от агента" in body


def test_specs_block_renders_without_scores(client) -> None:
    # Никакого _seed()/db.store_scores() -- d.scores должен остаться None, чтобы
    # реально проверить ветку "{% if not s %}": include стоит ДО этого гейта
    # именно затем, чтобы блок «Компьютер» был виден и до расчёта скорингов.
    db.touch_device("card-16", _iso_now(), "0.1.0", hostname="CARD-16")
    db.store_inventory("card-16", _iso_now(), _INV)
    body = client.get("/device/card-16").text
    assert "Intel(R) Core(TM) i5-10400" in body
