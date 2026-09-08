"""Разбор частичного отказа, отсрочка неизвестного узла и подавление шума.

Zabbix в ответе называет только числа («processed: 42; failed: 14»), имён он
не сообщает. Эти тесты проверяют, что оператор всё равно получает имя узла
и ключ — иначе искать причину пришлось бы вслепую.
"""

from __future__ import annotations

import pytest
from server.zabbix import export as zx
from server.zabbix.config import ZabbixConfig


@pytest.fixture(autouse=True)
def _reset():
    zx.reset_for_tests()
    yield
    zx.reset_for_tests()


def _row(device_id: str, hostname: str) -> dict:
    return {
        "device_id": device_id,
        "hostname": hostname,
        "model": "OptiPlex",
        "chassis": "desktop",
        "org_code": "7",
        "score_ts": "2026-09-08T11:00:00+00:00",
        "state": "h1",
        "risk": 12.0,
        "days_left": None,
        "dominant_label": "Накопитель",
        "action": "наблюдать",
    }


def _fake_send(reject_host=None, reject_key=None):
    """Подменённый транспорт: считает отвергнутые значения без реальных сокетов."""
    calls: list = []

    def send(_cfg, batch):
        calls.append(list(batch))
        failed = sum(
            1
            for i in batch
            if (reject_host and i.host == reject_host) or (reject_key and i.key == reject_key)
        )
        return zx.sender.SendResult(processed=len(batch) - failed, failed=failed, total=len(batch))

    return send, calls


CFG = ZabbixConfig(server="10.0.0.5", timeout_sec=1.0)


# --------------------------------------------------------------------------- #
# Разбор называет узел и ключ
# --------------------------------------------------------------------------- #
def test_partial_rejection_names_the_unknown_host(monkeypatch, caplog):
    monkeypatch.setattr(
        zx.db, "get_fleet_verdicts", lambda: [_row("dev-1", "GOOD"), _row("dev-2", "BAD")]
    )
    send, _calls = _fake_send(reject_host="BAD")
    monkeypatch.setattr(zx, "_send", send)

    with caplog.at_level("WARNING", logger="srp.zabbix"):
        result = zx.run_export_cycle(CFG)

    assert result["rejected"] > 0
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "«BAD»" in messages
    assert "Allowed hosts" in messages, "причины должны перечисляться, а не утверждаться"


def test_diagnosis_probe_sends_one_value_per_host_not_the_whole_set(monkeypatch):
    """Иначе у исправных машин в истории Zabbix появлялись бы лишние точки."""
    monkeypatch.setattr(
        zx.db, "get_fleet_verdicts", lambda: [_row("dev-1", "GOOD"), _row("dev-2", "BAD")]
    )
    send, calls = _fake_send(reject_host="BAD")
    monkeypatch.setattr(zx, "_send", send)

    zx.run_export_cycle(CFG)

    probes = [
        c for c in calls[1:] if len(c) == 1 and c[0].key == "srp.state" and c[0].value == "unknown"
    ]
    assert len(probes) == 2, "проход А — по одному значению srp.state на узел"


def test_missing_item_key_is_named(monkeypatch, caplog):
    monkeypatch.setattr(zx.db, "get_fleet_verdicts", lambda: [_row("dev-1", "GOOD")])
    send, _calls = _fake_send(reject_key="srp.reason")
    monkeypatch.setattr(zx, "_send", send)

    with caplog.at_level("WARNING", logger="srp.zabbix"):
        zx.run_export_cycle(CFG)

    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "srp.reason" in messages


# --------------------------------------------------------------------------- #
# Окно молчания и отсрочка
# --------------------------------------------------------------------------- #
def test_second_failure_within_the_hour_does_not_re_run_the_diagnosis(monkeypatch):
    monkeypatch.setattr(
        zx.db, "get_fleet_verdicts", lambda: [_row("dev-1", "GOOD"), _row("dev-2", "BAD")]
    )
    send, calls = _fake_send(reject_host="BAD")
    monkeypatch.setattr(zx, "_send", send)

    zx.run_export_cycle(CFG)
    first_round = len(calls)
    zx.backoff.clear()  # чтобы BAD снова попал в пакет
    zx.run_export_cycle(CFG)

    # второй цикл — только основная отправка и счётчики, без проходов разбора
    assert len(calls) - first_round <= 2


def test_rejected_host_is_put_on_backoff_and_leaves_the_packet(monkeypatch):
    monkeypatch.setattr(
        zx.db, "get_fleet_verdicts", lambda: [_row("dev-1", "GOOD"), _row("dev-2", "BAD")]
    )
    send, calls = _fake_send(reject_host="BAD")
    monkeypatch.setattr(zx, "_send", send)

    zx.run_export_cycle(CFG)
    assert "BAD" in zx.backoff.active()

    calls.clear()
    result = zx.run_export_cycle(CFG)

    assert all(i.host != "BAD" for i in calls[0]), "отложенный узел не должен слаться"
    assert result["skipped"] == 1


def test_force_send_brings_a_fixed_host_back_immediately(monkeypatch):
    monkeypatch.setattr(
        zx.db, "get_fleet_verdicts", lambda: [_row("dev-1", "GOOD"), _row("dev-2", "BAD")]
    )
    send, calls = _fake_send(reject_host="BAD")
    monkeypatch.setattr(zx, "_send", send)
    zx.run_export_cycle(CFG)
    assert "BAD" in zx.backoff.active()

    ok_send, ok_calls = _fake_send()
    monkeypatch.setattr(zx, "_send", ok_send)
    zx.run_export_cycle(CFG, force=True)

    assert any(i.host == "BAD" for i in ok_calls[0])


# --------------------------------------------------------------------------- #
# Шум в журнале
# --------------------------------------------------------------------------- #
def test_the_same_connection_error_is_not_logged_every_cycle(monkeypatch, caplog):
    monkeypatch.setattr(zx.db, "get_fleet_verdicts", lambda: [_row("dev-1", "GOOD")])
    monkeypatch.setattr(
        zx,
        "_send",
        lambda _cfg, _batch: zx.sender.SendResult(
            error="Zabbix 10.0.0.5:10051 недоступен", error_kind=zx.sender.KIND_REFUSED
        ),
    )

    with caplog.at_level("WARNING", logger="srp.zabbix"):
        for _ in range(5):
            zx.run_export_cycle(CFG)

    complaints = [r for r in caplog.records if "недоступен" in r.getMessage()]
    assert len(complaints) == 1, "повторы должны подавляться, а не топить журнал"


def test_a_different_error_cause_is_logged_immediately(monkeypatch, caplog):
    monkeypatch.setattr(zx.db, "get_fleet_verdicts", lambda: [_row("dev-1", "GOOD")])
    refused = zx.sender.SendResult(error="отклонено", error_kind=zx.sender.KIND_REFUSED)
    timed_out = zx.sender.SendResult(error="не ответил", error_kind=zx.sender.KIND_TIMEOUT)
    answers = iter([refused, refused, timed_out])
    monkeypatch.setattr(zx, "_send", lambda _cfg, _batch: next(answers))

    with caplog.at_level("WARNING", logger="srp.zabbix"):
        for _ in range(3):
            zx.run_export_cycle(CFG)

    assert len([r for r in caplog.records if "не ответил" in r.getMessage()]) == 1


def test_recovery_is_always_announced(monkeypatch, caplog):
    monkeypatch.setattr(zx.db, "get_fleet_verdicts", lambda: [_row("dev-1", "GOOD")])
    bad = zx.sender.SendResult(error="отклонено", error_kind=zx.sender.KIND_REFUSED)
    good = zx.sender.SendResult(processed=1, failed=0, total=1)
    answers = iter([bad, good, good])
    monkeypatch.setattr(zx, "_send", lambda _cfg, _batch: next(answers))

    with caplog.at_level("INFO", logger="srp.zabbix"):
        zx.run_export_cycle(CFG)
        zx.run_export_cycle(CFG)

    assert any("восстановлена" in r.getMessage() for r in caplog.records)


def test_successful_cycle_reports_sent_and_accepted_separately(monkeypatch, caplog):
    monkeypatch.setattr(zx.db, "get_fleet_verdicts", lambda: [_row("dev-1", "GOOD")])
    send, _calls = _fake_send()
    monkeypatch.setattr(zx, "_send", send)

    with caplog.at_level("INFO", logger="srp.zabbix"):
        zx.run_export_cycle(CFG)

    line = next(r.getMessage() for r in caplog.records if "отправлено" in r.getMessage())
    assert "принято" in line and "пропущено" in line


def test_skipped_machines_are_named_in_the_log(monkeypatch, caplog):
    monkeypatch.setattr(zx.db, "get_fleet_verdicts", lambda: [_row("dev-1", "")])
    send, _calls = _fake_send()
    monkeypatch.setattr(zx, "_send", send)

    with caplog.at_level("WARNING", logger="srp.zabbix"):
        zx.run_export_cycle(CFG)

    assert any("dev-1" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# Статус для оператора
# --------------------------------------------------------------------------- #
def test_status_snapshot_shows_what_the_operator_needs(monkeypatch):
    monkeypatch.setattr(
        zx.db, "get_fleet_verdicts", lambda: [_row("dev-1", "GOOD"), _row("dev-2", "BAD")]
    )
    send, _calls = _fake_send(reject_host="BAD")
    monkeypatch.setattr(zx, "_send", send)

    zx.run_export_cycle(CFG)
    snap = zx.status.snapshot()

    assert snap["configured"] is True
    assert snap["target"] == "10.0.0.5:10051"
    assert snap["devices"] == 2
    assert snap["accepted"] < snap["sent"]
    assert snap["rejected_hosts"] == ["BAD"]
    assert snap["backoff_hosts"] == ["BAD"]


def test_identical_successful_cycles_are_not_logged_every_time(monkeypatch, caplog):
    """Спокойно работающий экспорт не должен писать 288 одинаковых строк в сутки."""
    monkeypatch.setattr(zx.db, "get_fleet_verdicts", lambda: [_row("dev-1", "GOOD")])
    send, _calls = _fake_send()
    monkeypatch.setattr(zx, "_send", send)

    with caplog.at_level("INFO", logger="srp.zabbix"):
        for _ in range(5):
            zx.run_export_cycle(CFG)

    lines = [r for r in caplog.records if "отправлено" in r.getMessage()]
    assert len(lines) == 1


def test_changed_numbers_are_logged_immediately(monkeypatch, caplog):
    rows = [_row("dev-1", "GOOD")]
    monkeypatch.setattr(zx.db, "get_fleet_verdicts", lambda: rows)
    send, _calls = _fake_send()
    monkeypatch.setattr(zx, "_send", send)

    with caplog.at_level("INFO", logger="srp.zabbix"):
        zx.run_export_cycle(CFG)
        rows.append(_row("dev-2", "GOOD-2"))
        zx.run_export_cycle(CFG)

    lines = [r for r in caplog.records if "отправлено" in r.getMessage()]
    assert len(lines) == 2, "изменение чисел должно печататься сразу"


def test_repeated_outage_summary_counts_attempts(monkeypatch, caplog):
    monkeypatch.setattr(zx.db, "get_fleet_verdicts", lambda: [_row("dev-1", "GOOD")])
    answers = iter(
        [
            zx.sender.SendResult(error="Zabbix недоступен", error_kind=zx.sender.KIND_REFUSED),
            zx.sender.SendResult(error="Zabbix не ответил", error_kind=zx.sender.KIND_TIMEOUT),
        ]
    )
    monkeypatch.setattr(zx, "_send", lambda _cfg, _batch: next(answers))

    with caplog.at_level("WARNING", logger="srp.zabbix"):
        zx.run_export_cycle(CFG)
        zx.run_export_cycle(CFG)

    assert any("попыток" in r.getMessage() for r in caplog.records)


def test_duration_reads_naturally_in_russian():
    assert zx._duration(45) == "45 с"
    assert zx._duration(12 * 60) == "12 мин"
    assert zx._duration(2 * 3600 + 5 * 60) == "2 ч 05 мин"
