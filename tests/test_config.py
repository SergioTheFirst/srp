"""P1 — secure server_url: no insecure default; operator must set it at install.

The agent previously defaulted ``server_url`` to a hard-coded public IP, so a
fresh deployment that forgot to configure a target silently phoned home to that
box. We remove the default: ``server_url`` is unset unless the operator sets it
(LAN is typical; a public IP stays a valid *explicit* choice, just never a
silent one).
"""

from __future__ import annotations

import json

import pytest
from client import agent as agent_mod
from client.config import ClientConfig, ConfigError, load_config, validate_runtime_config


def test_default_config_has_no_server_url() -> None:
    # The public-IP default is gone; unset means empty, not a silent target.
    assert ClientConfig().server_url == ""


def test_load_config_missing_file_leaves_server_url_unset(tmp_path) -> None:
    cfg = load_config(tmp_path / "config.json")
    assert cfg.server_url == ""
    assert cfg.device_id  # device id is still resolved + persisted on first run


def test_load_config_reads_server_url_from_file(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"server_url": "http://192.168.1.10:8000", "device_id": "d"}),
        encoding="utf-8",
    )
    assert load_config(path).server_url == "http://192.168.1.10:8000"


def test_validate_raises_when_server_url_empty() -> None:
    with pytest.raises(ConfigError):
        validate_runtime_config(ClientConfig(server_url=""))


def test_validate_raises_on_whitespace_server_url() -> None:
    with pytest.raises(ConfigError):
        validate_runtime_config(ClientConfig(server_url="   "))


def test_validate_accepts_lan_url() -> None:
    validate_runtime_config(ClientConfig(server_url="http://192.168.1.10:8000"))  # no raise


def test_validate_accepts_public_url() -> None:
    # A public IP is still a valid explicit choice -- only the silent default went away.
    validate_runtime_config(ClientConfig(server_url="http://212.42.56.189:8000"))  # no raise


def test_agent_exits_without_server_url(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_mod, "load_config", lambda: ClientConfig(server_url="", device_id="d")
    )
    with pytest.raises(SystemExit) as exc:
        agent_mod.main(["--once"])
    assert exc.value.code == 2


def test_agent_server_flag_satisfies_requirement(monkeypatch) -> None:
    ran: dict[str, object] = {}

    class _StubAgent:
        def __init__(self, cfg: ClientConfig) -> None:
            ran["url"] = cfg.server_url

        def run_once(self) -> None:
            ran["once"] = True

        def run_forever(self) -> None:  # pragma: no cover - not exercised here
            ran["forever"] = True

    monkeypatch.setattr(
        agent_mod, "load_config", lambda: ClientConfig(server_url="", device_id="d")
    )
    monkeypatch.setattr(agent_mod, "Agent", _StubAgent)
    agent_mod.main(["--server", "http://192.168.1.5:8000", "--once"])
    assert ran["url"] == "http://192.168.1.5:8000"
    assert ran.get("once") is True
