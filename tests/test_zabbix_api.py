"""Операторские поверхности: /api/v1/zabbix/status, кнопка «Отправить сейчас», карточка."""

from __future__ import annotations

import pytest
from server import limits
from server.zabbix import export as zx


@pytest.fixture(autouse=True)
def _reset():
    zx.reset_for_tests()
    yield
    zx.reset_for_tests()


# --------------------------------------------------------------------------- #
# Статус
# --------------------------------------------------------------------------- #
def test_status_says_not_configured_out_of_the_box(client):
    body = client.get("/api/v1/zabbix/status").json()

    assert body["configured"] is False
    assert body["target"] == ""
    assert body["started_at"], "оператор должен видеть, с какого момента счётчики"


def test_status_exposes_the_fields_the_operator_needs(client, monkeypatch):
    monkeypatch.setattr(zx.db, "get_fleet_verdicts", lambda: [])
    monkeypatch.setattr(zx, "_send", lambda _cfg, _batch: zx.sender.SendResult())
    zx.run_export_cycle(zx.ZabbixConfig(server="10.0.0.5"))

    body = client.get("/api/v1/zabbix/status").json()

    for field in (
        "configured",
        "target",
        "devices",
        "sent",
        "accepted",
        "rejected",
        "skipped",
        "unknown",
        "last_error",
        "last_success_at",
        "backoff_hosts",
    ):
        assert field in body, f"в статусе нет поля {field}"


# --------------------------------------------------------------------------- #
# Кнопка «Отправить сейчас»
# --------------------------------------------------------------------------- #
def test_send_now_without_an_address_reports_that_it_is_not_configured(client):
    body = client.post("/api/v1/zabbix/send").json()

    assert body == {"skipped": "not_configured"}


def test_send_now_is_rate_limited(client, monkeypatch):
    monkeypatch.setattr("server.api.check_rate_limit", lambda _key: False)

    resp = client.post("/api/v1/zabbix/send")

    assert resp.status_code == 429


def test_send_now_is_refused_in_the_read_only_demo(client, monkeypatch):
    monkeypatch.setattr(limits, "DEMO_MODE", True)

    resp = client.post("/api/v1/zabbix/send")

    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Карточка на /pipeline
# --------------------------------------------------------------------------- #
def test_pipeline_page_shows_the_export_card_as_disabled(client):
    html = client.get("/pipeline").text

    assert "Экспорт в Zabbix" in html
    assert "выключен в конфигурации" in html
    assert "zabbix.server" in html


def test_pipeline_card_shows_the_target_and_counters_when_configured(client, monkeypatch):
    monkeypatch.setattr(zx.db, "get_fleet_verdicts", lambda: [])
    monkeypatch.setattr(
        zx, "_send", lambda _cfg, _batch: zx.sender.SendResult(processed=3, failed=0, total=3)
    )
    zx.run_export_cycle(zx.ZabbixConfig(server="10.0.0.5"))

    html = client.get("/pipeline").text

    assert "10.0.0.5:10051" in html
    assert "отправлено / принято" in html
    assert "с запуска сервера" in html, "счётчики не переживают перезапуск — это должно быть видно"


def test_pipeline_card_shows_the_last_error(client, monkeypatch):
    monkeypatch.setattr(zx.db, "get_fleet_verdicts", lambda: [])
    monkeypatch.setattr(
        zx,
        "_send",
        lambda _cfg, _batch: zx.sender.SendResult(
            error="Zabbix 10.0.0.5:10051 недоступен: соединение отклонено",
            error_kind=zx.sender.KIND_REFUSED,
        ),
    )
    zx.run_export_cycle(zx.ZabbixConfig(server="10.0.0.5"))

    html = client.get("/pipeline").text

    assert "соединение отклонено" in html


def test_pipeline_card_has_a_send_now_button(client):
    html = client.get("/pipeline").text

    assert "Отправить сейчас" in html
    assert "/api/v1/zabbix/send" in html


def test_send_now_button_is_disabled_in_the_demo(client, monkeypatch):
    monkeypatch.setattr(limits, "DEMO_MODE", True)

    html = client.get("/pipeline").text

    assert "демо: только чтение" in html


def test_send_now_cannot_be_hammered_faster_than_the_minimum_interval(client, monkeypatch):
    """Эндпоинт неаутентифицирован, а один прогон — запрос по всей таблице scores."""
    monkeypatch.setattr(zx.db, "get_fleet_verdicts", lambda: [])
    monkeypatch.setattr(zx, "_send", lambda _cfg, _batch: zx.sender.SendResult())
    cfg = zx.ZabbixConfig(server="10.0.0.5")

    first = zx.run_export_cycle(cfg, force=True)
    second = zx.run_export_cycle(cfg, force=True)

    assert "throttled" not in first
    assert second == {"throttled": True}, "частый повтор ≠ «отправка уже идёт»"
