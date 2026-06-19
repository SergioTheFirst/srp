"""P1 — headless logging + identity for the agent running as a Windows service.

When the agent runs as a SYSTEM scheduled task there is no console, so stderr
logging is lost. ``setup_logging`` adds an opt-in rotating file handler, and the
startup line records the effective user so the operator can confirm the task is
actually running as SYSTEM (the whole point -- SYSTEM unblocks SMART/WMI).
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from typing import Iterator

import pytest
from client import agent as agent_mod
from client.config import ClientConfig


@pytest.fixture(autouse=True)
def _isolate_root_logging() -> Iterator[None]:
    """Save/restore root logging so each test starts clean and releases files.

    setup_logging mutates the root logger; on Windows an open RotatingFileHandler
    keeps the temp log locked, so handlers added during a test must be closed
    before tmp_path cleanup.
    """
    root = logging.getLogger()
    saved = root.handlers[:]
    saved_level = root.level
    for h in saved:
        root.removeHandler(h)
    yield
    for h in root.handlers[:]:
        h.close()
        root.removeHandler(h)
    for h in saved:
        root.addHandler(h)
    root.setLevel(saved_level)


def _file_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if isinstance(h, RotatingFileHandler)]


def test_setup_logging_console_only_by_default() -> None:
    agent_mod.setup_logging(verbose=False, log_file=None)
    handlers = logging.getLogger().handlers
    assert any(isinstance(h, logging.StreamHandler) for h in handlers)
    assert _file_handlers() == []


def test_setup_logging_skips_console_when_no_stderr(monkeypatch, tmp_path) -> None:
    """A windowed (no-console) PyInstaller build has sys.stderr is None; a plain
    StreamHandler there raises on every log line. With no stderr we add only the
    rotating file handler -- never a broken console handler.
    """
    monkeypatch.setattr(agent_mod.sys, "stderr", None)
    log = tmp_path / "headless.log"
    agent_mod.setup_logging(verbose=False, log_file=str(log))
    handlers = logging.getLogger().handlers
    assert not any(type(h) is logging.StreamHandler for h in handlers)
    assert len(_file_handlers()) == 1
    logging.getLogger("srp.agent").info("headless-line")  # must not raise / be swallowed
    assert "headless-line" in log.read_text(encoding="utf-8")


def test_setup_logging_adds_rotating_file_handler(tmp_path) -> None:
    log = tmp_path / "srp-agent.log"
    agent_mod.setup_logging(verbose=True, log_file=str(log))
    assert len(_file_handlers()) == 1
    logging.getLogger("srp.agent").info("hello-service")
    assert log.exists()
    assert "hello-service" in log.read_text(encoding="utf-8")


def test_setup_logging_is_idempotent(tmp_path) -> None:
    log = tmp_path / "a.log"
    agent_mod.setup_logging(False, str(log))
    agent_mod.setup_logging(False, str(log))
    assert len(_file_handlers()) == 1  # re-init must not stack duplicate handlers


def test_main_logs_effective_user(monkeypatch, tmp_path) -> None:
    log = tmp_path / "run.log"

    class _StubAgent:
        def __init__(self, cfg: ClientConfig) -> None:
            pass

        def run_once(self) -> None:
            pass

    monkeypatch.setattr(
        agent_mod, "load_config", lambda: ClientConfig(server_url="http://x:8000", device_id="d")
    )
    monkeypatch.setattr(agent_mod, "Agent", _StubAgent)
    monkeypatch.setattr(agent_mod.getpass, "getuser", lambda: "SYSTEM")

    agent_mod.main(["--once", "--log-file", str(log)])

    assert "user=SYSTEM" in log.read_text(encoding="utf-8")


def test_redacted_url_strips_userinfo() -> None:
    # Basic-auth credentials embedded in server_url must never reach the log.
    assert agent_mod._redacted_url("http://user:pass@host:8000") == "http://host:8000"
    assert agent_mod._redacted_url("http://host:8000/api") == "http://host:8000/api"
    assert agent_mod._redacted_url("https://u:p@10.0.0.5:8000") == "https://10.0.0.5:8000"
