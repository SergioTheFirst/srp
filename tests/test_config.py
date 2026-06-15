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
from client import config as config_mod
from client.config import (
    ClientConfig,
    ConfigError,
    load_config,
    resolve_device_id,
    validate_runtime_config,
)


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


def test_device_id_differs_for_clones_with_same_machine_guid() -> None:
    # Core fix: two machines cloned from one image share MachineGuid but have
    # distinct hostnames -> distinct device_id (no more silent row collision).
    guid = "11111111-2222-3333-4444-555555555555"
    id_a = resolve_device_id(guid, "WS-ACME-01")
    id_b = resolve_device_id(guid, "WS-ACME-02")
    assert id_a != id_b


def test_device_id_is_stable_for_same_machine() -> None:
    guid = "11111111-2222-3333-4444-555555555555"
    assert resolve_device_id(guid, "WS-ACME-01") == resolve_device_id(guid, "WS-ACME-01")


def test_device_id_is_case_insensitive_on_hostname() -> None:
    guid = "11111111-2222-3333-4444-555555555555"
    assert resolve_device_id(guid, "WS-ACME-01") == resolve_device_id(guid, "ws-acme-01")


def test_device_id_is_opaque_not_raw_guid() -> None:
    # The registry GUID must not leak verbatim in the id sent to the server.
    guid = "11111111-2222-3333-4444-555555555555"
    did = resolve_device_id(guid, "WS-ACME-01")
    assert guid not in did
    assert did.startswith("dev-")


def test_device_id_falls_back_to_uuid_without_machine_guid() -> None:
    # Non-Windows / unreadable registry -> random per-install id, still unique.
    did = resolve_device_id(None, "WS-ACME-01")
    assert did.startswith("agent-")


def test_load_config_resolves_device_id_from_guid_and_host(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(config_mod, "_machine_guid", lambda: "AAAA-BBBB")
    monkeypatch.setattr(config_mod, "_hostname", lambda: "WS-ACME-09")
    cfg = load_config(tmp_path / "config.json")
    assert cfg.device_id == resolve_device_id("AAAA-BBBB", "WS-ACME-09")


def test_load_config_keeps_persisted_device_id(tmp_path) -> None:
    # Stable identity: an already-persisted id is never re-derived (no fleet churn).
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"device_id": "legacy-fixed-id"}), encoding="utf-8")
    assert load_config(path).device_id == "legacy-fixed-id"


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
