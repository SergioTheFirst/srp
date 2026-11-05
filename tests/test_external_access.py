"""Интернет видит только три пути агента; дашборд для него «не существует» (404)."""

from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from server.config import ServerConfig
from server.external_access import is_internal
from server.main import create_app

from tests.conftest import envelope, healthy

_AGENT_PATHS = ("/api/v1/ingest", "/api/v1/agent/update", "/api/v1/agent/update/package")
_DASHBOARD_PATHS = ("/", "/fleet", "/api/v1/devices", "/deploy", "/api/v1/health")
_INTERNAL = (
    "10.1.2.3",
    "172.16.0.9",
    "172.31.255.1",
    "192.168.1.5",
    "127.0.0.1",
    "::1",
    "::ffff:192.168.1.5",
    "fe80::1",
    "fd00::5",
)
_EXTERNAL = (
    "203.0.113.7",
    "8.8.8.8",
    "100.64.0.1",
    "172.32.0.1",
    "2001:db8::1",
    "2001:0:4136:e378::1",
    "::ffff:8.8.8.8",
    "testclient",
    "",
    None,
)


def _app(tmp_path: Path, token: str):
    return create_app(
        ServerConfig(
            db_path=str(tmp_path / "srp.db"),
            org_directory_path=str(tmp_path / "org_directory.json"),
            updates_dir=str(tmp_path / "updates"),
            ingest_token=token,
        )
    )


@pytest.fixture
def make_client(tmp_path: Path) -> Iterator:
    opened = []

    def _make(host: str, token: str = "s3cret") -> TestClient:
        c = TestClient(_app(tmp_path / host.replace(":", "_"), token), client=(host, 40000))
        opened.append(c.__enter__())
        return c

    yield _make
    for c in opened:
        c.__exit__(None, None, None)


def _filtered(resp) -> bool:
    # Ответ фильтра -- голый 404 без тела; 404 самих эндпоинтов несёт JSON detail.
    return resp.status_code == 404 and resp.content == b""


@pytest.mark.parametrize("host", _INTERNAL)
def test_internal_addresses_are_internal(host: str) -> None:
    assert is_internal(host)


@pytest.mark.parametrize("host", _EXTERNAL)
def test_everything_else_is_external(host) -> None:
    assert not is_internal(host)


@pytest.mark.parametrize("host", ("192.168.1.5", "127.0.0.1", "::1", "10.0.0.7"))
def test_internal_client_reaches_dashboard(make_client, host: str) -> None:
    assert make_client(host).get("/").status_code == 200


@pytest.mark.parametrize("path", _DASHBOARD_PATHS)
@pytest.mark.parametrize("host", ("203.0.113.7", "8.8.8.8", "100.64.0.1"))
def test_external_client_gets_bare_404_on_dashboard(make_client, host: str, path: str) -> None:
    assert _filtered(make_client(host).get(path))


_TOKEN = {"X-SRP-Token": "s3cret"}


def _hit(c: TestClient, path: str, **kw):
    return c.request("POST" if path.endswith("ingest") else "GET", path, **kw)


@pytest.mark.parametrize("path", _AGENT_PATHS[1:])
def test_external_agent_with_token_reaches_the_update_paths(make_client, path: str) -> None:
    # Пакета нет -> 404 самого эндпоинта с JSON detail, не голый 404 фильтра.
    resp = make_client("203.0.113.7").get(path, headers=_TOKEN)
    assert not _filtered(resp) and resp.status_code == 404 and resp.json()["detail"]


def test_external_agent_ingests_with_a_valid_token(make_client) -> None:
    c = make_client("203.0.113.7")
    body = envelope("ext-agent-001", "heartbeat", healthy("heartbeat"))
    resp = c.post("/api/v1/ingest", json=body, headers=_TOKEN)
    assert resp.status_code == 200, resp.text


def test_external_agent_path_with_query_string_passes(make_client) -> None:
    resp = make_client("8.8.8.8").get("/api/v1/agent/update/package?arch=x86", headers=_TOKEN)
    assert not _filtered(resp)


@pytest.mark.parametrize("token", (None, "", "wrong", "s3cret ", "секрет"))
@pytest.mark.parametrize("path", _AGENT_PATHS)
def test_external_without_the_token_gets_bare_404_before_any_body_work(
    make_client, path: str, token
) -> None:
    # Битый JSON: дойди запрос до pydantic, был бы 422 -- значит, тело даже не читали.
    headers = {} if token is None else {"X-SRP-Token": token.encode()}
    resp = _hit(make_client("203.0.113.7"), path, content=b"{not json" * 1000, headers=headers)
    assert _filtered(resp)


def test_internal_caller_keeps_the_endpoint_own_401(make_client) -> None:
    assert make_client("192.168.1.5").get("/api/v1/agent/update").status_code == 401


def test_external_token_holder_is_rate_limited_per_ip(make_client, monkeypatch) -> None:
    from server import external_access

    monkeypatch.setattr(external_access, "EXTERNAL_MAX_PER_MINUTE", 3)
    c = make_client("203.0.113.9")
    codes = [c.get("/api/v1/agent/update", headers=_TOKEN).status_code for _ in range(4)]
    assert codes[:3] == [404, 404, 404] and codes[3] == 429
    last = c.get("/api/v1/agent/update", headers=_TOKEN)
    assert last.status_code == 429 and int(last.headers["Retry-After"]) >= 1
    # Другой IP -- своё окно; LAN лимитом по IP не задет.
    assert (
        make_client("203.0.113.10").get("/api/v1/agent/update", headers=_TOKEN).status_code == 404
    )
    assert make_client("192.168.1.5").get("/api/v1/agent/update", headers=_TOKEN).status_code == 404


def test_rejected_external_requests_do_not_spend_the_ip_budget(make_client, monkeypatch) -> None:
    from server import external_access

    monkeypatch.setattr(external_access, "EXTERNAL_MAX_PER_MINUTE", 2)
    c = make_client("203.0.113.11")
    for _ in range(5):
        assert _filtered(c.get("/api/v1/agent/update", headers={"X-SRP-Token": "bad"}))
    assert c.get("/api/v1/agent/update", headers=_TOKEN).status_code == 404


def test_demo_mode_is_open_to_the_internet(make_client, monkeypatch) -> None:
    # Витрина на Vercel (api/index.py, SRP_DEMO=1) кладёт в scope публичный IP посетителя;
    # демо и так только для чтения (limits.DEMO_MODE -> 403 на изменяющих путях).
    from server import limits

    monkeypatch.setattr(limits, "DEMO_MODE", True)
    assert make_client("203.0.113.7").get("/").status_code == 200


@pytest.mark.parametrize("path", _AGENT_PATHS)
def test_empty_token_closes_external_access_entirely(make_client, path: str) -> None:
    c = make_client("203.0.113.7", token="")
    assert _filtered(_hit(c, path, headers={"X-SRP-Token": ""}))


def test_empty_token_keeps_lan_working(make_client) -> None:
    assert make_client("192.168.1.5", token="").get("/").status_code == 200


@pytest.mark.parametrize("path", ("/api/v1/ingest/", "/api/v1/agent/update/x", "/api/v1/Ingest"))
def test_external_only_exact_agent_paths(make_client, path: str) -> None:
    assert _filtered(make_client("8.8.8.8").get(path, headers=_TOKEN))


def test_forwarded_for_from_external_client_does_not_make_it_internal(make_client) -> None:
    c = make_client("203.0.113.7")
    resp = c.get("/", headers={"X-Forwarded-For": "192.168.1.5", "X-Real-IP": "127.0.0.1"})
    assert _filtered(resp)


def test_uvicorn_trusts_forwarded_for_only_from_localhost() -> None:
    # За Caddy на этом же хосте реальный IP приходит через X-Forwarded-For; uvicorn
    # принимает его только от 127.0.0.1 -- внешний клиент «внутренним» не притворится.
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    seen = {}

    async def app(scope, receive, send):
        seen["client"] = scope["client"]

    import asyncio

    mw = ProxyHeadersMiddleware(app, trusted_hosts="127.0.0.1")
    for peer, expected in ((("8.8.8.8", 1), "8.8.8.8"), (("127.0.0.1", 1), "192.168.1.5")):
        scope = {
            "type": "http",
            "client": peer,
            "headers": [(b"x-forwarded-for", b"192.168.1.5")],
            "scheme": "http",
        }
        asyncio.run(mw(scope, None, None))
        assert seen["client"][0] == expected


def test_main_trusts_forwarded_for_only_from_localhost(monkeypatch) -> None:
    import uvicorn
    from server import main as server_main

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(kw))
    monkeypatch.setattr(server_main, "_disable_quickedit", lambda: None)
    server_main.main()
    assert seen["forwarded_allow_ips"] == "127.0.0.1"
