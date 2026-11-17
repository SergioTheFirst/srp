"""Публичный стенд (SRP_DEMO=1) скрывает коэффициенты методики.

Веса каналов полноты, априорные и веса байеса, баллы факторов и улик, веса доменов
доверия и коды правил ступени -- константы калибровки (спека витрины 2026-11-04,
§8.0). Вердикт -- ступень, индекс, координаты, срок, причины словами -- остаётся.
Обычный сервер показывает всё, как раньше.
"""

from __future__ import annotations

import json

import pytest
from server import demo_redact, limits

from tests.test_device_hero import _real_health, _seed


def _risk() -> dict:
    health = _real_health(
        observability={
            "value": 90.0,
            "band": "good",
            "confidence": "high",
            "evidence": [{"label": "SMART диска (вес 0.35)", "delta": 0.35}],
        },
        damage={
            "value": 20.0,
            "band": "watch",
            "confidence": "high",
            "evidence": [{"label": "124 переназначенных сектора", "delta": 12.5}],
        },
        state_evidence=[{"label": "прошивка предсказывает отказ диска", "delta": 40.0}],
        rung_source="flag:predict",
        held_source="flag:predict",
    )
    return {
        "health": health,
        "classes": [
            {
                "name": "storage",
                "label": "Накопитель",
                "level": "high",
                "probability": 0.62,
                "trust": "ok",
                "factors": [
                    {"label": "Базовый риск класса (prior)", "weight": -2.94},
                    {"label": "переназначенные секторы", "weight": 1.54},
                ],
            },
            # no evidence: its probability IS the prior
            {"name": "memory", "label": "Память (RAM)", "level": "low", "probability": 0.02},
        ],
        "top": "storage",
        "overall": 62.0,
        "day1_factors": {
            "performance": [],
            "reliability": [{"label": "BSOD за 30 дней", "delta": -12.0}],
            "wear": [],
            "risk_exposure": [],
        },
        "domains": {"storage": {"state": "ok", "weight": 1.0}},
        "score100": {
            "reliability": {"value": 70, "factors": [{"label": "сбои", "delta": -8.0}]},
            "storage_risk": {"value": 75, "factors": [{"label": "предсказан отказ", "delta": 75}]},
        },
    }


def test_redact_drops_methodology_numbers_and_keeps_the_verdict() -> None:
    risk = _risk()
    out = demo_redact.redact(risk)
    text = json.dumps(out, ensure_ascii=False)
    assert "(вес" not in text and "prior" not in text and "rung_source" not in text
    assert '"weight"' not in text and '"delta"' not in text and "held_source" not in text
    assert '"probability"' not in text and '"overall"' not in text
    health = out["health"]
    assert health["observability"]["evidence"] == [{"label": "SMART диска"}]
    assert health["damage"]["evidence"] == [{"label": "124 переназначенных сектора"}]
    assert out["classes"][0]["factors"] == [{"label": "переназначенные секторы"}]
    assert out["domains"]["storage"] == {"state": "ok"}
    assert out["classes"][1] == {"name": "memory", "label": "Память (RAM)", "level": "low"}
    assert out["top"] == "storage" and out["score100"]["storage_risk"]["value"] == 75
    kept = (health["state"], health["index"], health["observability"]["value"], health["action"])
    assert kept == ("h1", 78.0, 90.0, risk["health"]["action"])
    assert health["delta_7d"] == -3.5  # change of the index, not a coefficient
    assert risk["classes"][0]["factors"][0]["weight"] == -2.94  # the input is untouched


def test_api_hides_coefficients_on_the_public_stand(client, monkeypatch) -> None:
    _seed("redact-1", "REDACT-1", _risk())
    monkeypatch.setattr(limits, "DEMO_MODE", True)
    for path in (
        "/api/v1/devices/redact-1",
        "/api/v1/devices/redact-1/health",
        "/api/v1/devices",
        "/api/v1/diagnostics/redact-1",
    ):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert "(вес" not in resp.text and "prior" not in resp.text, path
        assert '"weight"' not in resp.text and '"delta"' not in resp.text, path
        assert "rung_source" not in resp.text and "held_source" not in resp.text, path
        assert '"probability"' not in resp.text and '"overall"' not in resp.text, path
    assert client.get("/api/v1/diagnostics/redact-1").json()["storage_risk"]["value"] == 75


def test_api_keeps_coefficients_on_a_real_server(client) -> None:
    _seed("redact-2", "REDACT-2", _risk())
    text = client.get("/api/v1/devices/redact-2").text
    assert "(вес 0.35)" in text and '"weight"' in text


@pytest.mark.parametrize("demo", [True, False])
def test_device_page_prints_coefficients_only_on_a_real_server(client, monkeypatch, demo) -> None:
    _seed(f"redact-p{int(demo)}", "REDACT-P", _risk())
    monkeypatch.setattr(limits, "DEMO_MODE", demo)
    body = client.get(f"/device/redact-p{int(demo)}").text
    shown = ("(вес 0.35)" in body, "Базовый риск класса" in body, "+1.54" in body, "-12.0" in body)
    assert shown == ((False,) * 4 if demo else (True,) * 4)
    assert "SMART диска" in body and "переназначенные секторы" in body
