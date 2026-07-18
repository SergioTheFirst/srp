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


def test_agent_version_is_contract_compatible():
    """AGENT_VERSION (software release) and CONTRACT_VERSION (wire schema) are
    independent axes -- T6 bumped AGENT_VERSION to 0.2.0 for the auto-update
    feature while CONTRACT_VERSION stayed 0.1.0 (additive/optional fields never
    bump it). The invariant that must hold is MAJOR compatibility, not equality:
    a same-MAJOR agent's envelopes always parse (shared.schema.is_contract_compatible)."""
    from shared.schema import is_contract_compatible

    assert is_contract_compatible(AGENT_VERSION)


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


def test_envelope_includes_source_health(tmp_path):
    t, _ = _make(tmp_path)
    env = t._envelope("heartbeat", {}, {"free_space": {"status": "ok"}})
    assert env["source_health"]["free_space"]["status"] == "ok"


def test_envelope_source_health_defaults_empty(tmp_path):
    t, _ = _make(tmp_path)
    env = t._envelope("heartbeat", {"cpu_pct": 1.0})
    assert env["source_health"] == {}


def test_send_payload_none_with_health_is_delivered(tmp_path, monkeypatch):
    """No payload but a source is down -> still send, so the server learns it."""
    t, _ = _make(tmp_path)
    sent: dict = {}
    monkeypatch.setattr(t, "_deliver", lambda env: sent.update(env) or True)
    assert t.send("heartbeat", None, {"throttle": {"status": "timeout"}}) is True
    assert sent["payload"] == {}
    assert sent["source_health"]["throttle"]["status"] == "timeout"


# --------------------------------------------------------------------------- #
# P1-1: offline_mode must actually prevent network calls, not just skip the
# server_url validation. With server_url="" (the documented offline setup),
# urllib.request.Request(self._ingest_url, ...) raises ValueError('unknown url
# type') building a request for a schemeless relative URL -- outside the
# try/except in _attempt(), so it used to propagate and crash the caller.
# --------------------------------------------------------------------------- #
def _make_offline(tmp_path):
    cfg = ClientConfig(
        server_url="",  # documented offline setup -- no server configured at all
        offline_mode=True,
        device_id="t-dev",
        buffer_path=str(tmp_path / "buffer.jsonl"),
    )
    return Transport(cfg), cfg


def test_offline_mode_send_does_not_raise_on_empty_server_url(tmp_path):
    t, _ = _make_offline(tmp_path)
    assert t._ingest_url == "/api/v1/ingest"  # the malformed relative URL
    # Real Transport, real urllib -- no monkeypatching. This must not raise.
    assert t.send("heartbeat", {"cpu_pct": 1.0}) is False
    lines = t._read_buffer()
    assert len(lines) == 1  # buffered, not discarded -- flushes once online
    assert json.loads(lines[0])["msg_type"] == "heartbeat"


def test_offline_mode_flush_does_not_raise_on_a_preexisting_backlog(tmp_path):
    """offline_mode flipped on later with envelopes already buffered from a
    prior (online) run must not crash flush_buffer() either -- same _attempt()
    path, reached from flush_buffer -> _deliver instead of send -> _deliver."""
    t, _ = _make_offline(tmp_path)
    t._append_buffer(t._envelope("heartbeat", {"n": 0}))
    assert t.flush_buffer() == 0  # nothing delivered while offline
    assert len(t._read_buffer()) == 1  # still queued, untouched


def test_offline_mode_never_reaches_urlopen(tmp_path, monkeypatch):
    """Belt-and-suspenders: prove the network layer itself is never touched,
    not just that no exception happens to surface."""
    t, _ = _make_offline(tmp_path)
    called = {"n": 0}
    monkeypatch.setattr(
        transport_mod.urllib.request, "urlopen", lambda *a, **k: called.__setitem__("n", 1)
    )
    t.send("heartbeat", {"cpu_pct": 1.0})
    assert called["n"] == 0
