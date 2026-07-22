"""P1 — secure server_url: no insecure default; operator must set it at install.

The agent previously defaulted ``server_url`` to a hard-coded public IP, so a
fresh deployment that forgot to configure a target silently phoned home to that
box. We remove the default: ``server_url`` is unset unless the operator sets it
(LAN is typical; a public IP stays a valid *explicit* choice, just never a
silent one).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

import pytest
from client import agent as agent_mod
from client import config as config_mod
from client.config import (
    ClientConfig,
    ConfigError,
    _type_matches,
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
    # Two machines cloned from one image share MachineGuid but have distinct
    # hostnames -> distinct device_id (no silent row collision).
    guid = "11111111-2222-3333-4444-555555555555"
    id_a = resolve_device_id(guid, "WS-ACME-01")
    id_b = resolve_device_id(guid, "WS-ACME-02")
    assert id_a != id_b


def test_device_id_differs_for_clones_with_same_guid_and_hostname() -> None:
    # Worst case: mass-imaged clones share BOTH MachineGuid and the (not-yet-
    # renamed) hostname. The per-install nonce must still keep ids unique --
    # device_id is the server PRIMARY KEY, uniqueness is the hard requirement.
    guid = "11111111-2222-3333-4444-555555555555"
    ids = {resolve_device_id(guid, "WS-DEFAULT") for _ in range(1000)}
    assert len(ids) == 1000


def test_device_id_without_guid_is_unique_per_install() -> None:
    # Random fallback id must also never repeat across machines.
    ids = {resolve_device_id(None, "WS-DEFAULT") for _ in range(1000)}
    assert len(ids) == 1000


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
    path = tmp_path / "config.json"
    cfg = load_config(path)
    # Derived on first run (opaque, hashed) and then persisted, so a reload
    # returns the SAME id -- stable across restarts despite the random nonce.
    assert cfg.device_id.startswith("dev-")
    assert load_config(path).device_id == cfg.device_id


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


# ---------------------------------------------------------------------------
# P2-14 -- load_config() had no error handling around json.loads (a truncated
# or corrupted config.json crashed the agent at startup instead of falling
# back to defaults), _persist() wrote non-atomically (a power-cut mid-write
# could corrupt/truncate the file), and setattr() from the loaded JSON never
# validated the value's type against the dataclass field -- a wrong-type value
# (e.g. a string for heartbeat_interval_sec) was silently assigned and crashed
# later, far from the actual root cause (client/agent.py:106,
# ``int(getattr(self._cfg, interval_attr))``).
# ---------------------------------------------------------------------------


def test_load_config_survives_corrupted_json(tmp_path, caplog) -> None:
    path = tmp_path / "config.json"
    # Truncated mid-object: what a power-cut / disk-full write leaves behind.
    path.write_text('{"server_url": "http://x.test"  BROKEN', encoding="utf-8")
    with caplog.at_level("ERROR", logger="client.config"):
        cfg = load_config(path)
    assert cfg.server_url == ""  # fell back to defaults, not a half-parsed value
    assert cfg.device_id  # startup still completes: id resolved + persisted
    assert "config.json" in caplog.text


def test_load_config_survives_unreadable_file(tmp_path, caplog, monkeypatch) -> None:
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    original_read_text = Path.read_text

    def flaky_read_text(self, *args, **kwargs):
        if self == path:
            raise OSError("permission denied")
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)
    with caplog.at_level("ERROR", logger="client.config"):
        cfg = load_config(path)
    assert cfg.server_url == ""


def test_load_config_survives_non_object_json(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")  # valid JSON, but not an object
    cfg = load_config(path)  # must not raise (no .items() on a list)
    assert cfg.server_url == ""
    assert cfg.device_id


def test_load_config_rejects_wrong_type_for_int_field(tmp_path, caplog) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"heartbeat_interval_sec": "not-a-number", "device_id": "d"}),
        encoding="utf-8",
    )
    with caplog.at_level("ERROR", logger="client.config"):
        cfg = load_config(path)
    assert cfg.heartbeat_interval_sec == 14400  # default kept, bad value skipped
    assert "heartbeat_interval_sec" in caplog.text


def test_load_config_rejects_bool_for_int_field(tmp_path) -> None:
    # bool is a subclass of int in Python -- must not silently pass as one.
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"heartbeat_interval_sec": True, "device_id": "d"}), encoding="utf-8"
    )
    cfg = load_config(path)
    assert cfg.heartbeat_interval_sec == 14400


def test_load_config_rejects_int_for_bool_field(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"offline_mode": 1, "device_id": "d"}), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.offline_mode is False


def test_load_config_accepts_correct_type_for_int_field(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"heartbeat_interval_sec": 60, "device_id": "d"}), encoding="utf-8")
    cfg = load_config(path)
    assert cfg.heartbeat_interval_sec == 60


def test_load_config_ignores_unknown_keys(tmp_path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"not_a_real_field": 123, "device_id": "d"}), encoding="utf-8")
    cfg = load_config(path)  # must not raise
    assert not hasattr(cfg, "not_a_real_field")


def test_persist_writes_via_temp_file_then_replace(tmp_path, monkeypatch) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"device_id": "original"}', encoding="utf-8")
    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy_replace(src, dst):
        calls.append((Path(src).name, Path(dst).name))
        real_replace(src, dst)

    monkeypatch.setattr(config_mod.os, "replace", spy_replace)
    config_mod._persist(ClientConfig(device_id="new-id"), path)
    assert calls == [("config.json.tmp", "config.json")]
    assert json.loads(path.read_text(encoding="utf-8"))["device_id"] == "new-id"


def test_persist_interrupted_replace_leaves_original_file_untouched(tmp_path, monkeypatch) -> None:
    path = tmp_path / "config.json"
    path.write_text('{"device_id": "original"}', encoding="utf-8")

    def boom(src, dst) -> None:
        raise OSError("simulated power cut")

    monkeypatch.setattr(config_mod.os, "replace", boom)
    with pytest.raises(OSError):
        config_mod._persist(ClientConfig(device_id="new-id"), path)
    # The interrupted step was the replace itself -- the original file must
    # still hold its last-good content, never a torn/partial write.
    assert json.loads(path.read_text(encoding="utf-8"))["device_id"] == "original"


def test_load_config_survives_persist_failure_after_generating_device_id(
    tmp_path, monkeypatch, caplog
) -> None:
    path = tmp_path / "config.json"  # missing -> a fresh device_id is generated + persisted

    def boom(cfg, path) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(config_mod, "_persist", boom)
    with caplog.at_level("ERROR", logger="client.config"):
        cfg = load_config(path)
    assert cfg.device_id  # resolved in-memory even though the disk write failed


def test_load_config_survives_invalid_utf8(tmp_path, caplog) -> None:
    # A power-cut truncation landing mid-multibyte character (plausible: this
    # deployment stores Cyrillic comment/site_name/dept_code fields) yields
    # invalid UTF-8, not a JSONDecodeError -- a distinct exception type that
    # must not slip past the guard and crash startup either.
    path = tmp_path / "config.json"
    path.write_bytes(b'{"comment": "\xd0\x9f\xd0')
    with caplog.at_level("ERROR", logger="client.config"):
        cfg = load_config(path)
    assert cfg.server_url == ""  # fell back to defaults, not a half-parsed value
    assert cfg.device_id  # startup still completes
    assert "config.json" in caplog.text


def test_type_matches_skips_check_for_non_plain_type() -> None:
    # Every ClientConfig field today is a plain str/int/bool, so `expected` is
    # always a real class in practice -- but a subscripted generic like
    # Optional[X] isn't a class, and isinstance() against one is unreliable
    # across Python versions/typing constructs (can raise TypeError; observed
    # here to silently return False even for values that should be valid --
    # confirmed pre-fix: this exact assertion failed with `False is True`, not
    # a crash, on this runtime's typing.Optional). A future such field must
    # not turn this guard into a startup crash OR a silent always-reject;
    # skipping the strict check (accepting the value) is the safe fallback.
    assert _type_matches(Optional[int], "anything") is True
    assert _type_matches(Optional[int], 5) is True
    assert _type_matches(Optional[int], None) is True
