"""P1 — ingest authentication (optional shared token; empty = disabled, MVP-compatible)."""

from __future__ import annotations

import urllib.request

import pytest
from client.config import ClientConfig
from client.transport import Transport
from fastapi.testclient import TestClient
from server.config import ServerConfig
from server.main import create_app
from tests.conftest import envelope, healthy

pytestmark = pytest.mark.integration


def _token_app(tmp_path):
    return create_app(ServerConfig(db_path=str(tmp_path / "t.db"), ingest_token="secret"))


def test_ingest_rejects_without_or_wrong_token(tmp_path):
    with TestClient(_token_app(tmp_path)) as c:
        env = envelope("d", "heartbeat", healthy("heartbeat"))
        assert c.post("/api/v1/ingest", json=env).status_code == 401
        assert (
            c.post("/api/v1/ingest", json=env, headers={"X-SRP-Token": "wrong"}).status_code == 401
        )


def test_ingest_accepts_correct_token(tmp_path):
    with TestClient(_token_app(tmp_path)) as c:
        env = envelope("d", "heartbeat", healthy("heartbeat"))
        r = c.post("/api/v1/ingest", json=env, headers={"X-SRP-Token": "secret"})
        assert r.status_code == 200, r.text


def test_ingest_open_when_no_token_configured(client):
    # conftest `client` builds the app with default ingest_token="" -> auth disabled.
    env = envelope("d", "heartbeat", healthy("heartbeat"))
    assert client.post("/api/v1/ingest", json=env).status_code == 200


def test_transport_sends_token_header_when_set(tmp_path, monkeypatch):
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["token"] = req.get_header("X-srp-token")
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    cfg = ClientConfig(
        server_url="http://x/",
        device_id="d",
        ingest_token="secret",
        buffer_path=str(tmp_path / "b.jsonl"),
    )
    t = Transport(cfg)
    assert t._attempt(t._envelope("heartbeat", {"cpu_pct": 1.0})) == "ok"
    assert captured["token"] == "secret"


def test_transport_omits_token_header_when_empty(tmp_path, monkeypatch):
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["token"] = req.get_header("X-srp-token")
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    t = Transport(
        ClientConfig(server_url="http://x/", device_id="d", buffer_path=str(tmp_path / "b.jsonl"))
    )
    assert t._attempt(t._envelope("heartbeat", {"cpu_pct": 1.0})) == "ok"
    assert captured["token"] is None
