"""Конфигурация экспорта в Zabbix: безопасный дефолт и починка мусора.

Главный инвариант: без адреса экспорт выключен. Если бы он был включён по
умолчанию, каждый из ~40 существующих интеграционных тестов, поднимающих
TestClient(app), полез бы в сеть.
"""

from __future__ import annotations

import json
from pathlib import Path

from server import config as server_config
from server.config import ServerConfig, load_config
from server.zabbix.config import ZabbixConfig, load_zabbix_config


def test_no_block_means_disabled():
    cfg = load_zabbix_config(None)

    assert cfg.enabled is False
    assert cfg.server == ""


def test_empty_address_means_disabled_even_with_other_settings():
    cfg = load_zabbix_config({"port": 10051, "interval_sec": 900, "server": "   "})

    assert cfg.enabled is False


def test_non_empty_address_is_the_only_switch():
    cfg = load_zabbix_config({"server": "10.0.0.5"})

    assert cfg.enabled is True
    assert cfg.port == 10051 and cfg.interval_sec == 300


def test_server_config_defaults_to_disabled_export():
    assert ServerConfig().zabbix_config().enabled is False


def test_shipped_config_json_parses_and_ships_without_an_address():
    """Поставляемый конфиг несёт блок (фича не спящая), но адресата не задаёт."""
    shipped = Path(server_config.__file__).with_name("config.json")
    data = json.loads(shipped.read_text(encoding="utf-8"))

    assert "zabbix" in data, "блок zabbix должен присутствовать в поставляемом конфиге"
    assert load_zabbix_config(data["zabbix"]).enabled is False


# --------------------------------------------------------------------------- #
# Клэмпы и починка мусора
# --------------------------------------------------------------------------- #
def test_interval_is_clamped_from_below():
    assert load_zabbix_config({"server": "h", "interval_sec": 5}).interval_sec == 60


def test_garbage_interval_falls_back_to_default_without_crashing():
    assert load_zabbix_config({"server": "h", "interval_sec": "часто"}).interval_sec == 300


def test_timeout_is_clamped_to_a_sane_window():
    assert load_zabbix_config({"server": "h", "timeout_sec": 0.01}).timeout_sec == 1.0
    assert load_zabbix_config({"server": "h", "timeout_sec": 9999}).timeout_sec == 60.0


def test_port_out_of_range_falls_back_to_the_default():
    assert load_zabbix_config({"server": "h", "port": 0}).port == 10051
    assert load_zabbix_config({"server": "h", "port": 99999}).port == 10051
    assert load_zabbix_config({"server": "h", "port": "десять"}).port == 10051


def test_unknown_host_field_falls_back_to_hostname():
    assert load_zabbix_config({"server": "h", "host_field": "серийник"}).host_field == "hostname"


def test_unknown_settings_are_ignored_not_fatal():
    """Старые ключи из прошлых версий конфига не должны ронять загрузку."""
    cfg = load_zabbix_config({"server": "h", "items": "full", "base_url": "http://x"})

    assert cfg.enabled is True


def test_host_map_keeps_only_usable_pairs():
    cfg = load_zabbix_config(
        {"server": "h", "host_map": {"dev-1": "INV-4471", "dev-2": "", "": "X", "dev-3": 5}}
    )

    assert cfg.host_map == {"dev-1": "INV-4471"}


def test_host_map_of_the_wrong_shape_is_ignored_not_fatal():
    assert load_zabbix_config({"server": "h", "host_map": ["dev-1"]}).host_map == {}


def test_org_codes_are_normalised_to_strings():
    cfg = load_zabbix_config({"server": "h", "org_codes": ["7", 8, "", None]})

    assert cfg.org_codes == ("7", "8")


# --------------------------------------------------------------------------- #
# Предупреждение об отсутствии шифрования
# --------------------------------------------------------------------------- #
def test_local_address_does_not_warn_about_encryption():
    assert load_zabbix_config({"server": "10.0.0.5"}).encryption_warning() is None
    assert load_zabbix_config({"server": "127.0.0.1"}).encryption_warning() is None
    assert load_zabbix_config({"server": "192.168.1.9"}).encryption_warning() is None


def test_public_address_warns_that_the_trapper_is_not_encrypted():
    warning = load_zabbix_config({"server": "8.8.8.8"}).encryption_warning()

    assert warning is not None
    assert "не шифруется" in warning


def test_dns_name_asks_the_operator_to_check():
    warning = load_zabbix_config({"server": "zabbix.corp.local"}).encryption_warning()

    assert warning is not None
    assert "именем" in warning


def test_disabled_export_never_warns():
    assert ZabbixConfig().encryption_warning() is None


# --------------------------------------------------------------------------- #
# Живая загрузка файла
# --------------------------------------------------------------------------- #
def test_block_is_read_from_a_config_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"zabbix": {"server": "10.0.0.5", "host_suffix": ".corp"}}), encoding="utf-8"
    )

    cfg = load_config(path).zabbix_config()

    assert cfg.server == "10.0.0.5" and cfg.host_suffix == ".corp"
