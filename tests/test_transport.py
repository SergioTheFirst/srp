"""Client transport tests: envelope shape + offline buffer (no real network).

Network I/O is stubbed by monkeypatching ``_deliver`` so these stay fast and
deterministic. They lock the zero-data-loss contract: transient failures buffer
FIFO and stop at the first blocked envelope; corrupt lines are dropped.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest
from client import transport as transport_mod
from client.config import ClientConfig
from client.transport import AGENT_VERSION, CONTRACT_VERSION, Transport
from shared.schema import CONTRACT_VERSION as SERVER_CONTRACT

pytestmark = pytest.mark.unit


def _make(tmp_path):
    cfg = ClientConfig(
        server_url="http://127.0.0.1:9/",  # nominal; network is stubbed in tests
        device_id="t-dev",
        buffer_path=str(tmp_path / "buffer.jsonl"),
    )
    return Transport(cfg), cfg


def test_the_agent_declares_a_compatible_contract():
    """AGENT_VERSION (software release) and CONTRACT_VERSION (wire schema) are independent
    axes. Since stage 5 the 406 gate reads the declared contract_version, so AGENT_VERSION
    1.0.0 has its own MAJOR; what must hold is that the declared contract is compatible."""
    from shared.schema import is_contract_compatible

    assert is_contract_compatible(CONTRACT_VERSION)


def test_agent_version_matches_version_file():
    """D1 (исследование автообновления, 2026-08-22): рассинхрон версий не поймать.

    Версия живёт в ДВУХ местах: transport.AGENT_VERSION (зашита в бинарник, её
    агент сообщает серверу и по ней updater.reconcile_after_restart судит об
    успехе) и корневой файл VERSION (его build.bat кладёт в шару, а
    packaging/make_update_package.py — в пакет обновления). Нигде не сверялись:
    при рассинхроне агент после успешного обновления продолжает представляться
    старой версией, reconcile считает обновление несостоявшимся и сжигает все
    _MAX_ATTEMPTS попытки на уже установленную версию. Этот пин ловит рассинхрон
    на гейте — до сборки и выкладки.
    """
    version_file = Path(__file__).resolve().parents[1] / "VERSION"
    assert version_file.read_text(encoding="utf-8").strip() == AGENT_VERSION


def test_send_none_payload_is_noop(tmp_path):
    t, _ = _make(tmp_path)
    assert t.send("heartbeat", None) is True
    assert not t._buffer.exists()


def test_envelope_has_required_fields(tmp_path):
    t, cfg = _make(tmp_path)
    env = t._envelope("heartbeat", {"cpu_pct": 1.0})
    assert env["device_id"] == cfg.device_id
    assert env["agent_version"] == AGENT_VERSION
    assert env["contract_version"] == SERVER_CONTRACT  # stage 5: the 406 gate reads it
    assert env["msg_type"] == "heartbeat"
    assert env["payload"] == {"cpu_pct": 1.0}
    assert "T" in env["ts"]  # ISO timestamp


def test_the_agent_speaks_the_servers_contract():
    """The stdlib agent keeps its own copy of the contract version (it cannot import
    pydantic's shared.schema) -- the two must never drift."""
    assert CONTRACT_VERSION == SERVER_CONTRACT


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


def test_failed_liveness_leaves_no_buffer_file(tmp_path, monkeypatch):
    """0.3.4 #1: a replayed ping says nothing (the server stamps last_seen on receipt),
    yet pings were 91 % of buffered lines -- the cap evicted real data after ~16 days."""
    t, _ = _make(tmp_path)
    monkeypatch.setattr(t, "_deliver", lambda env: False)  # server unreachable
    assert t.send("liveness", {"alive": True}) is False
    assert not t._buffer.exists()


def test_buffered_liveness_is_purged_with_zero_posts(tmp_path, monkeypatch):
    """0.3.3 backlogs: pings already on disk leave on the next flush, no network call."""
    t, _ = _make(tmp_path)
    for _ in range(2):
        t._append_buffer(t._envelope("liveness", {"alive": True}))
    posts: list = []

    def _down(*a, **_kw):
        posts.append(a)
        raise urllib.error.URLError("down")

    monkeypatch.setattr(transport_mod.urllib.request, "urlopen", _down)
    monkeypatch.setattr(transport_mod.time, "sleep", lambda _s: None)
    assert t.flush_buffer() == 2
    assert posts == []
    assert not t._buffer.exists()


def test_outage_logs_one_warning_then_one_recovery_line(tmp_path, monkeypatch, caplog):
    """0.3.4 #4: every failed attempt was a WARNING -- 85 % of the 4 x 1 MB log was
    'network error sending', so the lines explaining a dark PC rotated out in days."""
    t, _ = _make(tmp_path)
    replies = iter([urllib.error.URLError("refused"), urllib.error.URLError("refused"), None, None])

    def _urlopen(*_a, **_kw):
        reply = next(replies)
        if reply is None:
            return contextlib.nullcontext()
        raise reply

    monkeypatch.setattr(transport_mod.urllib.request, "urlopen", _urlopen)
    with caplog.at_level(logging.DEBUG, logger="client.transport"):
        outcomes = [t._attempt(t._envelope("heartbeat", {})) for _ in range(4)]
    records = [r for r in caplog.records if r.name == "client.transport"]
    assert outcomes == ["retry", "retry", "ok", "ok"]
    assert [r.levelname for r in records] == ["WARNING", "DEBUG", "INFO"]
    assert "связь восстановлена" in records[2].getMessage()


def test_no_recovery_line_after_an_error_that_was_not_an_outage(tmp_path, monkeypatch, caplog):
    """Ревью 0.3.4: 4xx-отказ или 429 -- сервер отвечал, связь не рвалась; «связь
    восстановлена» после них путала разбор журнала."""
    t, _ = _make(tmp_path)

    def _http(code):
        # настоящее пустое тело: на Python 3.9 HTTPError(fp=None) роняет read() с KeyError
        return urllib.error.HTTPError("http://x", code, "x", {}, io.BytesIO(b""))  # type: ignore[arg-type]

    replies = iter([_http(400), None, _http(429), None])

    def _urlopen(*_a, **_kw):
        reply = next(replies)
        if reply is None:
            return contextlib.nullcontext()
        raise reply

    monkeypatch.setattr(transport_mod.urllib.request, "urlopen", _urlopen)
    with caplog.at_level(logging.DEBUG, logger="client.transport"):
        for _ in range(2):
            t._attempt(t._envelope("heartbeat", {}))
        t._throttled_until = 0.0
        for _ in range(2):
            t._attempt(t._envelope("heartbeat", {}))
    assert not any("связь восстановлена" in r.getMessage() for r in caplog.records)


def test_replay_carries_a_fresh_sent_ts_and_the_original_envelope(tmp_path, monkeypatch):
    """0.3.4 #9: ts is collection time and stays old on a replay -- the server took the
    buffer's age for clock drift («рассинхронизация часов», minus 10 points)."""
    t, _ = _make(tmp_path)
    env = t._envelope("heartbeat", {"cpu_pct": 1.0})
    env["ts"] = "2026-01-01T00:00:00+00:00"  # collected long ago, buffered since
    t._append_buffer(env)
    bodies: list[dict] = []

    def _ok(req, **_kw):
        bodies.append(json.loads(req.data))
        return contextlib.nullcontext()

    monkeypatch.setattr(transport_mod.urllib.request, "urlopen", _ok)
    assert t.flush_buffer() == 1
    sent_ts = bodies[0].pop("sent_ts")
    assert bodies[0] == env  # ts, idempotency_key and the rest go out untouched
    age = datetime.now(timezone.utc) - datetime.fromisoformat(sent_ts)
    assert abs(age.total_seconds()) < 60


def test_every_attempt_is_stamped_but_the_buffer_line_is_not(tmp_path, monkeypatch):
    t, _ = _make(tmp_path)
    posted: list[dict] = []

    def _down(req, **_kw):
        posted.append(json.loads(req.data))
        raise urllib.error.URLError("down")

    monkeypatch.setattr(transport_mod.urllib.request, "urlopen", _down)
    monkeypatch.setattr(transport_mod.time, "sleep", lambda _s: None)
    assert t.send("heartbeat", {"cpu_pct": 1.0}) is False
    assert len(posted) == 2
    assert all("sent_ts" in body for body in posted)
    assert "sent_ts" not in json.loads(t._read_buffer()[0])


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


def test_offline_mode_drops_oversized_payload(tmp_path):
    """o5-C9: негабарит отбрасывается ДО буферизации -- иначе офлайн-агент
    копит на диске конверты, которые сервер гарантированно отвергнет."""
    t, cfg = _make_offline(tmp_path)
    big = {"blob": "x" * (transport_mod._MAX_PAYLOAD_BYTES + 1)}
    assert t.send("historical", big) is True  # «обработан» = отброшен
    assert t.buffer_depth() == 0


def test_buffered_envelopes_of_a_previous_device_id_are_dropped_unsent(tmp_path, monkeypatch):
    """0.3.5: после смены device_id (сброс клона, потеря config.json) буфер под старым id
    не досылается -- иначе старая запись «оживает» поверх новой (spec canonical-devices)."""
    t, cfg = _make(tmp_path)
    t._append_buffer({**t._envelope("heartbeat", {"cpu_pct": 1.0}), "device_id": "old-dev"})
    posts: list = []

    def _accept(*a, **_kw):
        posts.append(a)
        raise AssertionError("must not POST an envelope of another device_id")

    monkeypatch.setattr(transport_mod.urllib.request, "urlopen", _accept)
    assert t.flush_buffer() == 1
    assert posts == []
    assert not t._buffer.exists()


def test_envelope_carries_the_build_bitness(tmp_path):
    """0.3.6: дашборд сверяет x86-агента с x86-пакетом -- разрядность в каждом конверте."""
    import struct

    from client.winflags import AGENT_ARCH
    from shared.schema import Envelope

    t, _ = _make(tmp_path)
    env = t._envelope("heartbeat", {})
    assert env["agent_arch"] == AGENT_ARCH == ("x86" if struct.calcsize("P") == 4 else "x64")
    assert Envelope(**env).agent_arch == AGENT_ARCH
