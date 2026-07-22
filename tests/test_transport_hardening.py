"""P1 transport hardening: reconnect jitter, idempotency dedup, rate-limit, body size.

RED phase: each test is written against the behaviour we WANT; all should fail
until the matching implementation is in place.

Unit tests (pure Python, no HTTP):
  - Envelope carries a unique idempotency_key per build.
  - Retry sleep includes random jitter (sleep > _RETRY_BACKOFF_SEC).

Integration tests (FastAPI TestClient):
  - Duplicate envelope (same idempotency_key) returns 200 with duplicate:true.
  - A business-validation failure (422) does not burn the idempotency key --
    retry with corrected content is processed, not dropped as a duplicate.
  - Rate-limited device returns 429 after _RATE_MAX_PER_WINDOW requests.
  - Request body exceeding the size limit returns 413.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from server.config import ServerConfig
from server.main import create_app
from tests.conftest import envelope, healthy

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _app(tmp_path):
    return create_app(ServerConfig(db_path=str(tmp_path / "t.db")))


def _make_transport(tmp_path):
    from client.config import ClientConfig
    from client.transport import Transport

    cfg = ClientConfig(
        server_url="http://127.0.0.1:9/",
        device_id="t-dev",
        buffer_path=str(tmp_path / "buf.jsonl"),
    )
    return Transport(cfg)


# --------------------------------------------------------------------------- #
# Unit: envelope idempotency_key
# --------------------------------------------------------------------------- #


def test_envelope_has_idempotency_key(tmp_path):
    """Every envelope built by the transport must carry a 32-char hex idempotency key."""
    t = _make_transport(tmp_path)
    env = t._envelope("heartbeat", {"cpu_pct": 1.0})
    key = env.get("idempotency_key")
    assert key is not None, "idempotency_key missing from envelope"
    assert isinstance(key, str)
    assert len(key) == 32
    assert all(c in "0123456789abcdef" for c in key)


def test_each_envelope_gets_unique_key(tmp_path):
    """Two separate envelope builds must produce different idempotency keys."""
    t = _make_transport(tmp_path)
    k1 = t._envelope("heartbeat", {})["idempotency_key"]
    k2 = t._envelope("heartbeat", {})["idempotency_key"]
    assert k1 != k2


def test_buffered_envelope_retains_key(tmp_path, monkeypatch):
    """When an envelope is buffered and later re-read, the key is preserved."""
    import json

    t = _make_transport(tmp_path)
    env = t._envelope("heartbeat", {"cpu_pct": 3.0})
    original_key = env["idempotency_key"]
    t._append_buffer(env)
    lines = t._read_buffer()
    recovered = json.loads(lines[0])
    assert recovered["idempotency_key"] == original_key


# --------------------------------------------------------------------------- #
# Unit: retry jitter
# --------------------------------------------------------------------------- #


def test_retry_sleep_includes_jitter(tmp_path, monkeypatch):
    """Retry backoff sleep must be RETRY_BACKOFF_SEC + random jitter, not flat."""
    import client.transport as tm

    sleep_calls: list[float] = []
    monkeypatch.setattr(tm.time, "sleep", lambda s: sleep_calls.append(s))
    monkeypatch.setattr(tm.random, "uniform", lambda a, b: 1.5)  # fixed jitter

    t = _make_transport(tmp_path)
    monkeypatch.setattr(t, "_attempt", lambda env: "retry")  # always fail
    t._deliver(t._envelope("heartbeat", {}))

    # _SEND_ATTEMPTS=2 → sleep called once between the two attempts
    assert len(sleep_calls) == 1
    # sleep = _RETRY_BACKOFF_SEC + jitter; jitter mocked as 1.5 → 2.5
    assert sleep_calls[0] == pytest.approx(tm._RETRY_BACKOFF_SEC + 1.5)


def test_jitter_constant_is_positive():
    """_RETRY_JITTER_SEC must be defined and > 0 so jitter actually randomises backoff."""
    import client.transport as tm

    assert hasattr(tm, "_RETRY_JITTER_SEC"), "_RETRY_JITTER_SEC constant missing"
    assert tm._RETRY_JITTER_SEC > 0


# --------------------------------------------------------------------------- #
# Unit: ingest_guards module
# --------------------------------------------------------------------------- #


def test_idempotency_new_key_is_not_seen():
    from server.ingest_guards import has_seen, reset_guards

    reset_guards()
    assert has_seen("aabbccdd" * 4) is False


def test_idempotency_marked_key_is_seen():
    from server.ingest_guards import has_seen, mark_seen, reset_guards

    reset_guards()
    key = "deadbeef" * 4
    assert has_seen(key) is False
    mark_seen(key)
    assert has_seen(key) is True  # duplicate


def test_idempotency_none_key_never_marks():
    """Agents that don't send a key (old agents) must never be blocked."""
    from server.ingest_guards import has_seen, mark_seen, reset_guards

    reset_guards()
    assert has_seen(None) is False
    mark_seen(None)
    assert has_seen(None) is False  # None never deduplicates


def test_rate_limit_allows_within_window(monkeypatch):
    from server import ingest_guards
    from server.ingest_guards import check_rate_limit, reset_guards

    monkeypatch.setattr(ingest_guards, "_RATE_MAX_PER_WINDOW", 3)
    reset_guards()
    assert check_rate_limit("dev-x") is True
    assert check_rate_limit("dev-x") is True
    assert check_rate_limit("dev-x") is True


def test_rate_limit_blocks_excess(monkeypatch):
    from server import ingest_guards
    from server.ingest_guards import check_rate_limit, reset_guards

    monkeypatch.setattr(ingest_guards, "_RATE_MAX_PER_WINDOW", 3)
    reset_guards()
    for _ in range(3):
        check_rate_limit("dev-y")
    assert check_rate_limit("dev-y") is False


def test_rate_limit_independent_per_device(monkeypatch):
    from server import ingest_guards
    from server.ingest_guards import check_rate_limit, reset_guards

    monkeypatch.setattr(ingest_guards, "_RATE_MAX_PER_WINDOW", 2)
    reset_guards()
    check_rate_limit("a")
    check_rate_limit("a")
    # "a" is exhausted, "b" starts fresh
    assert check_rate_limit("a") is False
    assert check_rate_limit("b") is True


def test_device_windows_trims_stale_entries_past_threshold():
    """stoperrors P2-7: _device_windows must not grow unboundedly for devices that
    stop sending -- mirrors the existing _seen_keys opportunistic trim (mark_seen,
    above). Seeds > _TRIM_THRESHOLD devices with an aged-out timestamp each, then
    confirms one more real call shrinks the dict instead of leaving it to grow
    forever (today's code, pre-fix, never removes a device once added)."""
    import time

    from server import ingest_guards
    from server.ingest_guards import check_rate_limit, reset_guards

    reset_guards()
    # Guaranteed older than any cutoff check_rate_limit computes below, regardless
    # of how long the seeding loop takes (monotonic clock never goes backwards).
    old_ts = time.monotonic() - ingest_guards._RATE_WINDOW_SEC - 1.0
    n = ingest_guards._TRIM_THRESHOLD + 1  # > 50_000, per stoperrors P2-7
    for i in range(n):
        ingest_guards._device_windows[f"stale-{i}"] = [old_ts]

    check_rate_limit("fresh-device")  # dict is over threshold -> trim should fire

    assert len(ingest_guards._device_windows) == 1
    assert list(ingest_guards._device_windows) == ["fresh-device"]


# --------------------------------------------------------------------------- #
# Unit: client-side payload size cap
# --------------------------------------------------------------------------- #


def test_oversized_payload_dropped_not_buffered(tmp_path) -> None:
    from types import SimpleNamespace

    from client import transport as tr

    cfg = SimpleNamespace(
        server_url="http://127.0.0.1:9",  # discard-порт: сеть не должна понадобиться
        offline_mode=False,
        device_id="d1",
        hostname="h",
        site_code="",
        site_name="",
        org_code="",
        dept_code="",
        comment="",
        ingest_token="",
        http_timeout_sec=1.0,
        resolved_buffer_path=lambda: tmp_path / "buffer.jsonl",
    )
    t = tr.Transport(cfg)

    big = {"blob": "x" * (tr._MAX_PAYLOAD_BYTES + 1)}
    assert t.send("historical", big) is True  # «обработан» = отброшен без ретраев
    assert t.buffer_depth() == 0  # и НЕ лёг в оффлайн-буфер
    assert "cap" in t.last_error


# --------------------------------------------------------------------------- #
# Integration: HTTP behaviour
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_duplicate_envelope_returns_200_with_flag(tmp_path):
    """POSTing the same idempotency_key twice: second returns 200 duplicate:true."""
    from server.ingest_guards import reset_guards

    reset_guards()
    with TestClient(_app(tmp_path)) as c:
        env = envelope("dup-dev", "heartbeat", healthy("heartbeat"))
        env["idempotency_key"] = "cafebabe" * 4
        r1 = c.post("/api/v1/ingest", json=env)
        assert r1.status_code == 200
        assert r1.json().get("duplicate") is not True  # first is processed

        r2 = c.post("/api/v1/ingest", json=env)
        assert r2.status_code == 200
        assert r2.json().get("duplicate") is True  # second is a dup


@pytest.mark.integration
def test_422_does_not_burn_idempotency_key(tmp_path):
    """stoperrors P1-3: a business-validation failure (422) must not permanently
    mark the key as seen -- retrying with corrected content must be processed,
    not silently dropped as a false duplicate (permanent data loss)."""
    from server.ingest_guards import reset_guards

    reset_guards()
    with TestClient(_app(tmp_path)) as c:
        key = "0badf00d" * 4
        bad = envelope("p13-dev", "heartbeat", {"cpu_pct": "not-a-number"})
        bad["idempotency_key"] = key
        r1 = c.post("/api/v1/ingest", json=bad)
        assert r1.status_code == 422

        good = envelope("p13-dev", "heartbeat", healthy("heartbeat"))
        good["idempotency_key"] = key
        r2 = c.post("/api/v1/ingest", json=good)
        assert r2.status_code == 200, r2.text
        assert r2.json().get("duplicate") is not True


@pytest.mark.integration
def test_rate_limited_device_returns_429(tmp_path, monkeypatch):
    """After _RATE_MAX_PER_WINDOW ingest calls, next returns HTTP 429."""
    from server import ingest_guards
    from server.ingest_guards import reset_guards

    monkeypatch.setattr(ingest_guards, "_RATE_MAX_PER_WINDOW", 3)
    reset_guards()
    with TestClient(_app(tmp_path)) as c:
        for i in range(3):
            env = envelope("rate-dev", "heartbeat", healthy("heartbeat"))
            env["idempotency_key"] = f"aaaa{i:028x}"  # unique key each time
            r = c.post("/api/v1/ingest", json=env)
            assert r.status_code == 200, f"request {i} failed: {r.text}"

        env = envelope("rate-dev", "heartbeat", healthy("heartbeat"))
        env["idempotency_key"] = "ffff" + "0" * 28
        r = c.post("/api/v1/ingest", json=env)
        assert r.status_code == 429


@pytest.mark.integration
def test_oversized_body_returns_413(tmp_path):
    """A request body exceeding the server limit must be rejected with 413."""
    from server.ingest_guards import reset_guards

    reset_guards()
    # Build a payload large enough to trip the 512 KB limit
    big_env = envelope("big-dev", "heartbeat", healthy("heartbeat"))
    big_env["_filler"] = "x" * (600 * 1024)  # ~600 KB
    with TestClient(_app(tmp_path)) as c:
        r = c.post(
            "/api/v1/ingest",
            content=__import__("json").dumps(big_env).encode(),
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 413
