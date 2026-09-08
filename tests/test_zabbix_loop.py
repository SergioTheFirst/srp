"""Проводка цикла экспорта: гейт, самозащита, лок и перечитывание конфига."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from server import limits, main
from server.config import ServerConfig
from server.zabbix import export as zx
from server.zabbix.config import ZabbixConfig

from tests.zabbix_trapper_stub import TrapperStub


@pytest.fixture(autouse=True)
def _reset():
    zx.reset_for_tests()
    yield
    zx.reset_for_tests()


def _rows(monkeypatch, rows):
    monkeypatch.setattr(zx.db, "get_fleet_verdicts", lambda: rows)


ROW = {
    "device_id": "dev-1",
    "hostname": "BUH-01",
    "model": "OptiPlex",
    "chassis": "desktop",
    "org_code": "7",
    "score_ts": "2026-09-08T11:00:00+00:00",
    "state": "h2",
    "risk": 30.0,
    "days_left": 90,
    "dominant_label": "Накопитель",
    "action": "наблюдать",
}


# --------------------------------------------------------------------------- #
# Гейт: без адреса ничего не происходит
# --------------------------------------------------------------------------- #
def test_cycle_without_an_address_does_nothing(monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(zx.db, "get_fleet_verdicts", lambda: called.__setitem__("n", 1) or [])

    result = zx.run_export_cycle(ZabbixConfig())

    assert result == {"skipped": "not_configured"}
    assert called["n"] == 0, "без адреса база не должна даже читаться"


def test_not_configured_is_announced_once_not_every_cycle(caplog):
    with caplog.at_level("INFO", logger="srp.zabbix"):
        zx.run_export_cycle(ZabbixConfig())
        zx.run_export_cycle(ZabbixConfig())

    said = [r for r in caplog.records if "не настроен" in r.getMessage()]
    assert len(said) == 1


def test_public_address_is_warned_about_once(monkeypatch, caplog):
    """Публичный адресат + нешифрованный траппер = предупреждение при настройке."""
    _rows(monkeypatch, [])
    monkeypatch.setattr(zx, "_send", lambda _cfg, _batch: zx.sender.SendResult())
    cfg = ZabbixConfig(server="8.8.8.8", timeout_sec=1.0)

    with caplog.at_level("WARNING", logger="srp.zabbix"):
        zx.run_export_cycle(cfg)
        zx.run_export_cycle(cfg)

    warned = [r for r in caplog.records if "не шифруется" in r.getMessage()]
    assert len(warned) == 1


# --------------------------------------------------------------------------- #
# Самозащита обёртки в main.py
# --------------------------------------------------------------------------- #
def test_glue_swallows_any_exception(monkeypatch, caplog):
    def boom(_cfg, **_kw):
        raise RuntimeError("внезапно")

    monkeypatch.setattr(main.zabbix_export, "run_export_cycle", boom)

    with caplog.at_level("ERROR", logger="srp.zabbix"):
        main._run_zabbix_export(ServerConfig())  # не должно бросить

    assert any("zabbix export cycle failed" in r.getMessage() for r in caplog.records)


def test_glue_does_not_read_the_repo_config_for_an_explicit_server_config():
    """Тестовый ServerConfig не должен подхватывать боевой server/config.json."""
    cfg = ServerConfig(zabbix={"server": "10.9.9.9"})

    resolved = main.zabbix_config_for(cfg, from_disk=False)

    assert resolved.server == "10.9.9.9"


def test_broken_config_file_falls_back_to_the_previous_settings(monkeypatch, caplog):
    def boom():
        raise ValueError("битый JSON")

    monkeypatch.setattr(main, "load_config", boom)
    cfg = ServerConfig(zabbix={"server": "10.9.9.9"})

    with caplog.at_level("ERROR", logger="srp.zabbix"):
        resolved = main.zabbix_config_for(cfg, from_disk=True)

    assert resolved.server == "10.9.9.9"


def test_address_edited_on_disk_is_picked_up_without_a_restart(monkeypatch, tmp_path):
    """Правка адреса не требует перезапуска: конфиг перечитывается каждый цикл."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"zabbix": {"server": "10.0.0.5"}}), encoding="utf-8")
    monkeypatch.setenv("SRP_CONFIG_PATH", str(path))

    first = main.zabbix_config_for(ServerConfig(), from_disk=True)
    path.write_text(json.dumps({"zabbix": {"server": "10.0.0.9"}}), encoding="utf-8")
    second = main.zabbix_config_for(ServerConfig(), from_disk=True)

    assert (first.server, second.server) == ("10.0.0.5", "10.0.0.9")


# --------------------------------------------------------------------------- #
# Лок «одна отправка за раз»
# --------------------------------------------------------------------------- #
def test_second_concurrent_send_gets_busy_instead_of_stacking(monkeypatch):
    _rows(monkeypatch, [ROW])
    seen = {}

    with TrapperStub("ok") as stub:
        cfg = ZabbixConfig(server="127.0.0.1", port=stub.port, timeout_sec=2.0)

        original = zx.items_mod.heartbeat_items

        def reentrant(*a, **kw):
            seen["inner"] = zx.run_export_cycle(cfg)
            return original(*a, **kw)

        monkeypatch.setattr(zx.items_mod, "heartbeat_items", reentrant)
        zx.run_export_cycle(cfg)

    assert seen["inner"] == {"busy": True}


def test_force_clears_the_backoff_list(monkeypatch):
    _rows(monkeypatch, [ROW])
    zx.backoff.add(["BUH-01"])
    assert "BUH-01" in zx.backoff.active()

    with TrapperStub("ok") as stub:
        cfg = ZabbixConfig(server="127.0.0.1", port=stub.port, timeout_sec=2.0)
        zx.run_export_cycle(cfg, force=True)

    assert zx.backoff.active() == set()


# --------------------------------------------------------------------------- #
# Стартовая проводка
# --------------------------------------------------------------------------- #
def test_loop_is_scheduled_at_startup(monkeypatch, tmp_path):
    started = {"n": 0}

    async def fake_loop(_cfg, **_kw):
        started["n"] += 1
        await asyncio.sleep(3600)

    monkeypatch.setattr(main, "_zabbix_export_loop", fake_loop)
    app = main.create_app(ServerConfig(db_path=str(tmp_path / "t.db")))
    with TestClient(app):
        pass

    assert started["n"] == 1


def test_demo_mode_silences_the_loop(monkeypatch, tmp_path):
    started = {"n": 0}

    async def fake_loop(_cfg, **_kw):
        started["n"] += 1
        await asyncio.sleep(3600)

    monkeypatch.setattr(main, "_zabbix_export_loop", fake_loop)
    monkeypatch.setattr(limits, "DEMO_MODE", True)
    monkeypatch.setattr(main.limits, "DEMO_MODE", True)
    app = main.create_app(ServerConfig(db_path=str(tmp_path / "t2.db")))
    with TestClient(app):
        pass

    assert started["n"] == 0
