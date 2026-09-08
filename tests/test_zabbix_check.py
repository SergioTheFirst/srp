"""Утилита `python -m server.zabbix.check` — проверка настройки до включения."""

from __future__ import annotations

import pytest
from server.zabbix import check as chk
from server.zabbix import export as zx
from server.zabbix.config import ZabbixConfig

from tests.zabbix_trapper_stub import TrapperStub

ROWS = [
    {
        "device_id": "dev-1",
        "hostname": "BUH-01",
        "model": "OptiPlex",
        "chassis": "desktop",
        "org_code": "7",
        "score_ts": "2026-09-08T11:00:00+00:00",
        "state": "h3",
        "risk": 61.0,
        "days_left": 30,
        "dominant_label": "Накопитель",
        "action": "заменить диск",
    },
    {
        "device_id": "dev-2",
        "hostname": "",
        "model": "",
        "chassis": "",
        "org_code": "7",
        "score_ts": "2026-09-08T11:00:00+00:00",
        "state": "h0",
        "risk": 3.0,
        "days_left": None,
        "dominant_label": "",
        "action": "",
    },
]


@pytest.fixture(autouse=True)
def _rows(monkeypatch):
    monkeypatch.setattr(chk.db, "get_fleet_verdicts", lambda: ROWS)
    monkeypatch.setattr(chk.db, "init_db", lambda *_a, **_kw: None)
    zx.reset_for_tests()
    yield
    zx.reset_for_tests()


CFG = ZabbixConfig(server="10.0.0.5")


# --------------------------------------------------------------------------- #
# Список узлов под массовый импорт
# --------------------------------------------------------------------------- #
def test_list_hosts_prints_csv_ready_for_zabbix_import(capsys):
    chk.cmd_list_hosts(CFG)
    out = capsys.readouterr().out.splitlines()

    assert out[0] == "host,visible_name,device_id,skip_reason"
    assert "BUH-01,BUH-01,dev-1," in out[1]


def test_list_hosts_marks_machines_that_will_be_skipped(capsys):
    chk.cmd_list_hosts(CFG)
    out = capsys.readouterr().out

    assert "dev-2" in out
    assert "имя узла" in out, "причина пропуска должна быть видна прямо в файле"


def test_list_hosts_honours_the_suffix(capsys):
    chk.cmd_list_hosts(ZabbixConfig(server="z", host_suffix=".corp.local"))

    assert "BUH-01.corp.local" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Что уедет по одной машине
# --------------------------------------------------------------------------- #
def test_device_prints_the_exact_values_that_would_be_sent(capsys):
    rc = chk.cmd_device(CFG, "BUH-01")
    out = capsys.readouterr().out

    assert rc == 0
    assert '"srp.state"' in out and '"h3"' in out
    assert "Накопитель: заменить диск" in out


def test_unknown_device_is_reported_not_crashed(capsys):
    rc = chk.cmd_device(CFG, "НЕТ-ТАКОЙ")

    assert rc == 1
    assert "не найдена" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Сухой прогон
# --------------------------------------------------------------------------- #
def test_dry_run_touches_no_network_and_counts_everything(capsys):
    rc = chk.cmd_dry_run(CFG)
    out = capsys.readouterr().out

    assert rc == 0
    assert "машин к отправке" in out
    assert "пропущено машин" in out
    assert "пропуск: dev-2" in out


# --------------------------------------------------------------------------- #
# Чек-лист
# --------------------------------------------------------------------------- #
def test_checklist_without_an_address_says_so_and_fails():
    assert chk.cmd_checklist(ZabbixConfig()) == 1


def test_checklist_passes_against_a_working_receiver(capsys):
    with TrapperStub("ok") as stub:
        cfg = ZabbixConfig(server="127.0.0.1", port=stub.port, timeout_sec=2.0)
        rc = chk.cmd_checklist(cfg)

    out = capsys.readouterr().out
    assert rc == 0
    assert "экспорт настроен верно" in out
    assert "хост службы" in out and "пробная машина" in out
    assert "время SRP" in out, "часы SRP нужно показать: траппер своё время не сообщает"


def test_checklist_reports_an_unknown_service_host(capsys):
    with TrapperStub("partial", reject=lambda i: i["host"] == "SRP") as stub:
        cfg = ZabbixConfig(server="127.0.0.1", port=stub.port, timeout_sec=2.0)
        rc = chk.cmd_checklist(cfg)

    out = capsys.readouterr().out
    assert rc == 1
    assert "«SRP»" in out and "не принял" in out


def test_checklist_warns_about_the_missing_encryption(capsys):
    with TrapperStub("ok") as stub:
        cfg = ZabbixConfig(server="127.0.0.1", port=stub.port, timeout_sec=2.0)
        chk.cmd_checklist(cfg)

    assert "шифрование" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# Какие узлы Zabbix не знает
# --------------------------------------------------------------------------- #
def test_check_hosts_names_the_hosts_zabbix_does_not_know(capsys):
    with TrapperStub("partial", reject=lambda i: i["host"] == "BUH-01") as stub:
        cfg = ZabbixConfig(server="127.0.0.1", port=stub.port, timeout_sec=2.0)
        rc = chk.cmd_check_hosts(cfg)

    out = capsys.readouterr().out
    assert rc == 1
    assert "Zabbix не знает узел: BUH-01" in out


def test_check_hosts_is_green_when_every_host_exists(capsys):
    with TrapperStub("ok") as stub:
        cfg = ZabbixConfig(server="127.0.0.1", port=stub.port, timeout_sec=2.0)
        rc = chk.cmd_check_hosts(cfg)

    assert rc == 0
    assert "известно 1 из 1" in capsys.readouterr().out


def test_check_hosts_stops_on_a_connection_error(capsys):
    rc = chk.cmd_check_hosts(ZabbixConfig(server="127.0.0.1", port=9, timeout_sec=1.0))

    assert rc == 1


# --------------------------------------------------------------------------- #
# Аргументы
# --------------------------------------------------------------------------- #
def test_server_argument_overrides_the_config_without_editing_it(monkeypatch):
    monkeypatch.setattr(chk, "load_config", lambda: _cfg_with(""))
    args = chk.build_parser().parse_args(["--server", "10.0.0.9", "--port", "10999"])

    cfg = chk._resolve_config(args)

    assert (cfg.server, cfg.port) == ("10.0.0.9", 10999)


def _cfg_with(server: str):
    class _Fake:
        def zabbix_config(self):
            return ZabbixConfig(server=server)

    return _Fake()


def test_main_dispatches_to_list_hosts(monkeypatch, capsys):
    monkeypatch.setattr(chk, "load_config", lambda: _FakeConfig())

    rc = chk.main(["--list-hosts"])

    assert rc == 0
    assert "host,visible_name,device_id,skip_reason" in capsys.readouterr().out


class _FakeConfig:
    def zabbix_config(self):
        return ZabbixConfig(server="10.0.0.5")

    def resolved_db_path(self):
        return "unused.db"
