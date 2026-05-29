"""Client transport tests: envelope shape + offline buffer (no real network).

Network I/O is stubbed by monkeypatching ``_deliver`` so these stay fast and
deterministic. They lock the zero-data-loss contract: transient failures buffer
FIFO and stop at the first blocked envelope; corrupt lines are dropped.
"""

from __future__ import annotations

import json

import pytest
from client import transport as transport_mod
from client.config import ClientConfig
from client.transport import AGENT_VERSION, Transport

pytestmark = pytest.mark.unit


def _make(tmp_path):
    cfg = ClientConfig(
        server_url="http://127.0.0.1:9/",  # nominal; network is stubbed in tests
        device_id="t-dev",
        buffer_path=str(tmp_path / "buffer.jsonl"),
    )
    return Transport(cfg), cfg


def test_agent_version_matches_contract():
    from shared.schema import CONTRACT_VERSION

    assert AGENT_VERSION == CONTRACT_VERSION


def test_send_none_payload_is_noop(tmp_path):
    t, _ = _make(tmp_path)
    assert t.send("heartbeat", None) is True
    assert not t._buffer.exists()


def test_envelope_has_required_fields(tmp_path):
    t, cfg = _make(tmp_path)
    env = t._envelope("heartbeat", {"cpu_pct": 1.0})
    assert env["device_id"] == cfg.device_id
    assert env["agent_version"] == AGENT_VERSION
    assert env["msg_type"] == "heartbeat"
    assert env["payload"] == {"cpu_pct": 1.0}
    assert "T" in env["ts"]  # ISO timestamp


def test_ingest_url_built_once(tmp_path):
    t, _ = _make(tmp_path)
    assert t._ingest_url == "http://127.0.0.1:9/api/v1/ingest"  # trailing slash collapsed


def test_offline_send_buffers_envelope(tmp_path, monkeypatch):
    t, _ = _make(tmp_path)
    monkeypatch.setattr(t, "_deliver", lambda env: False)  # server unreachable
    assert t.send("heartbeat", {"cpu_pct": 5.0}) is False
    lines = t._read_buffer()
    assert len(lines) == 1
    assert json.loads(lines[0])["msg_type"] == "heartbeat"


def test_flush_empty_buffer_returns_zero(tmp_path):
    t, _ = _make(tmp_path)
    assert t.flush_buffer() == 0


def test_flush_drains_fifo_when_server_back(tmp_path, monkeypatch):
    t, _ = _make(tmp_path)
    for i in range(3):
        t._append_buffer(t._envelope("heartbeat", {"n": i}))
    monkeypatch.setattr(t, "_deliver", lambda env: True)
    assert t.flush_buffer() == 3
    assert not t._buffer.exists()  # fully drained -> file removed


def test_flush_stops_at_first_transient_failure(tmp_path, monkeypatch):
    """Zero data loss: a mid-queue failure keeps that envelope and all after it."""
    t, _ = _make(tmp_path)
    for i in range(3):
        t._append_buffer(t._envelope("heartbeat", {"n": i}))

    calls = {"n": 0}

    def flaky(env):
        calls["n"] += 1
        return calls["n"] <= 2  # first two succeed, third blocks

    monkeypatch.setattr(t, "_deliver", flaky)
    assert t.flush_buffer() == 2
    remaining = t._read_buffer()
    assert len(remaining) == 1
    assert json.loads(remaining[0])["payload"]["n"] == 2  # the blocked one is kept


def test_flush_drops_corrupt_line(tmp_path, monkeypatch):
    t, _ = _make(tmp_path)
    t._buffer.parent.mkdir(parents=True, exist_ok=True)
    good = json.dumps(t._envelope("heartbeat", {"ok": True}))
    t._buffer.write_text("{ this is not json\n" + good + "\n", encoding="utf-8")
    monkeypatch.setattr(t, "_deliver", lambda env: True)
    # corrupt line counts as handled (dropped) + the good one delivered.
    assert t.flush_buffer() == 2
    assert not t._buffer.exists()


def test_buffer_is_trimmed_to_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(transport_mod, "_MAX_BUFFER_LINES", 3)
    t, _ = _make(tmp_path)
    for i in range(5):
        t._append_buffer(t._envelope("heartbeat", {"n": i}))
    lines = t._read_buffer()
    assert len(lines) == 3
    # oldest dropped -> the kept lines are the last three appended.
    assert [json.loads(ln)["payload"]["n"] for ln in lines] == [2, 3, 4]
