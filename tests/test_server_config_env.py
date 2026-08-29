"""Задача 3 (public-release): env-оверрайды server/config.py::load_config.

``PORT`` (Render инжектит его в контейнер) с фолбэком на ``SRP_PORT``, и
``SRP_DB_PATH`` (демо-контейнер указывает на одноразовую БД) -- по образцу
уже существующего ``SRP_ORG_DIRECTORY`` (config.py:17-18,126-128).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from server.config import ServerConfig, load_config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Не зависим от того, что хост/CI уже мог экспортировать.
    for name in ("PORT", "SRP_PORT", "SRP_DB_PATH"):
        monkeypatch.delenv(name, raising=False)


def test_no_env_vars_leaves_file_config_untouched(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"port": 9001, "db_path": "custom.db"}), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.port == 9001
    assert cfg.db_path == "custom.db"


def test_no_env_vars_leaves_defaults_untouched(tmp_path: Path) -> None:
    cfg = load_config(tmp_path / "missing.json")
    assert cfg.port == ServerConfig().port
    assert cfg.db_path == ServerConfig().db_path


def test_srp_port_sets_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SRP_PORT", "9090")
    cfg = load_config(tmp_path / "missing.json")
    assert cfg.port == 9090


def test_port_takes_precedence_over_srp_port(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PORT", "10000")
    monkeypatch.setenv("SRP_PORT", "9090")
    cfg = load_config(tmp_path / "missing.json")
    assert cfg.port == 10000


def test_srp_db_path_sets_db_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SRP_DB_PATH", str(tmp_path / "demo.db"))
    cfg = load_config(tmp_path / "missing.json")
    assert cfg.db_path.endswith("demo.db")


def test_empty_port_env_treated_as_not_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Render/Docker иногда выдают пустое значение вместо отсутствия переменной.
    monkeypatch.setenv("PORT", "")
    cfg = load_config(tmp_path / "missing.json")
    assert cfg.port == ServerConfig().port


def test_empty_port_falls_back_to_srp_port(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PORT", "")
    monkeypatch.setenv("SRP_PORT", "7000")
    cfg = load_config(tmp_path / "missing.json")
    assert cfg.port == 7000


def test_empty_db_path_env_treated_as_not_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SRP_DB_PATH", "")
    cfg = load_config(tmp_path / "missing.json")
    assert cfg.db_path == ServerConfig().db_path


@pytest.mark.parametrize("bad_value", ["abc", "0", "99999", "-1", "8080.5", "  "])
def test_malformed_port_is_rejected_not_crashed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    bad_value: str,
) -> None:
    # A typo'd env var must not kill startup with a bare ValueError traceback,
    # and must not silently accept a bogus/out-of-range port either -- it logs
    # and keeps whatever port was already resolved (file value or default).
    monkeypatch.setenv("PORT", bad_value)
    with caplog.at_level("ERROR", logger="srp.config"):
        cfg = load_config(tmp_path / "missing.json")
    assert cfg.port == ServerConfig().port
    assert "PORT" in caplog.text


def test_malformed_port_keeps_file_configured_port_not_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The fallback target is whatever load_config already resolved (the
    # config.json value), never blindly the dataclass default -- a malformed
    # env var must not clobber an otherwise-valid config.json.
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"port": 8500}), encoding="utf-8")
    monkeypatch.setenv("PORT", "not-a-port")
    cfg = load_config(path)
    assert cfg.port == 8500


def test_valid_port_range_boundaries_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PORT", "1")
    assert load_config(tmp_path / "missing.json").port == 1
    monkeypatch.setenv("PORT", "65535")
    assert load_config(tmp_path / "missing.json").port == 65535


# --------------------------------------------------------------------------- #
# SRP_CONFIG_PATH: на serverless-хостинге (Vercel) файловая система только для
# чтения, и приём Dockerfile «скопировать демо-конфиг поверх рабочего» не
# работает — конфиг выбирается переменной окружения.
# --------------------------------------------------------------------------- #


def test_config_path_env_selects_the_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    other = tmp_path / "demo-config.json"
    other.write_text(json.dumps({"port": 8123, "stale_after_sec": 3600}), encoding="utf-8")
    monkeypatch.setenv("SRP_CONFIG_PATH", str(other))
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("SRP_PORT", raising=False)
    cfg = load_config()
    assert cfg.port == 8123
    assert cfg.stale_after_sec == 3600


def test_explicit_path_argument_still_wins_over_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Явный аргумент сильнее окружения: иначе тесты и smoke, передающие путь,
    молча читали бы чужой конфиг."""
    env_cfg = tmp_path / "env.json"
    env_cfg.write_text(json.dumps({"port": 8001}), encoding="utf-8")
    explicit = tmp_path / "explicit.json"
    explicit.write_text(json.dumps({"port": 8002}), encoding="utf-8")
    monkeypatch.setenv("SRP_CONFIG_PATH", str(env_cfg))
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("SRP_PORT", raising=False)
    assert load_config(explicit).port == 8002
