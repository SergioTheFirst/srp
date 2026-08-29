"""Agent core-loop resilience: a broken Transport.send() must not kill the
loop -- same "a broken collector must not kill the loop" pattern already
applied to the collector/updater calls a few lines above each send() (P1-2).
"""

from __future__ import annotations

import pytest
from client.agent import Agent
from client.collectors.sources import CollectorResult
from client.config import ClientConfig

pytestmark = pytest.mark.unit


def _cfg(tmp_path) -> ClientConfig:
    return ClientConfig(
        server_url="http://127.0.0.1:9",
        device_id="t-dev",
        buffer_path=str(tmp_path / "buffer.jsonl"),
    )


def _raising_send(*args, **kwargs):
    # A realistic trigger per the plan: a collector payload with a value the
    # JSON encoder can't handle.
    raise TypeError("Object of type set is not JSON serializable")


def test_run_task_survives_a_broken_transport_send(tmp_path, monkeypatch):
    agent = Agent(_cfg(tmp_path))
    monkeypatch.setattr(agent._transport, "send", _raising_send)
    result = CollectorResult(payload={"cpu_pct": 1.0}, source_health={})
    agent._run_task("heartbeat", lambda: result)  # must not raise


def test_reconcile_update_survives_a_broken_transport_send(tmp_path, monkeypatch):
    agent = Agent(_cfg(tmp_path))
    monkeypatch.setattr(agent._transport, "send", _raising_send)
    monkeypatch.setattr(agent._updater, "reconcile_after_restart", lambda version: {"state": "ok"})
    agent._reconcile_update()  # must not raise


def test_run_update_check_survives_a_broken_transport_send(tmp_path, monkeypatch):
    agent = Agent(_cfg(tmp_path))
    monkeypatch.setattr(agent._transport, "send", _raising_send)
    monkeypatch.setattr(agent._updater, "check", lambda version: ({"state": "ok"}, False))
    restart = agent._run_update_check()  # must not raise
    assert restart is False


def test_run_task_still_delivers_when_transport_send_is_healthy(tmp_path, monkeypatch):
    """Regression guard: the try/except must not swallow a normal delivery."""
    agent = Agent(_cfg(tmp_path))
    calls: list[tuple] = []
    monkeypatch.setattr(agent._transport, "send", lambda *a: calls.append(a) or True)
    result = CollectorResult(payload={"cpu_pct": 1.0}, source_health={})
    agent._run_task("heartbeat", lambda: result)
    assert calls == [("heartbeat", {"cpu_pct": 1.0}, {})]
