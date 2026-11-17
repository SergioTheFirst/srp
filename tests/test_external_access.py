"""Интернет видит только три пути агента; дашборд для него «не существует» (404).

Порт прокси Caddy (8001) -- отдельный шлюз: вход по паролю, только чтение.
"""

from __future__ import annotations

import logging
import re
import socket
from pathlib import Path
from typing import Iterator
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from server import db, internet_view, users
from server.config import ServerConfig, load_config
from server.external_access import is_internal
from server.main import create_app
from starlette.websockets import WebSocketDisconnect

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


def _app(tmp_path: Path, token: str, **kw):
    return create_app(
        ServerConfig(
            db_path=str(tmp_path / "srp.db"),
            org_directory_path=str(tmp_path / "org_directory.json"),
            updates_dir=str(tmp_path / "updates"),
            ingest_token=token,
            **kw,
        )
    )


@pytest.fixture
def make_client(tmp_path: Path) -> Iterator:
    opened = []

    def _make(host: str, token: str = "s3cret", **kw) -> TestClient:
        c = TestClient(_app(tmp_path / host.replace(":", "_"), token), client=(host, 40000), **kw)
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


@pytest.mark.parametrize(
    ("iv", "port", "warned"),
    (
        (None, 8001, False),
        ({}, 8001, False),
        ({"enabled": True}, 8001, False),
        ({"proxy_port": 18001}, 18001, False),
        ({"enabled": False}, None, False),
        (False, None, False),
        ({"proxy_port": 8000}, None, True),  # = основному порту
        ({"proxy_port": "8001"}, None, True),
        ({"proxy_port": True}, None, True),
        ({"proxy_port": 70000}, None, True),
    ),
)
def test_internet_view_port(caplog, iv, port, warned: bool) -> None:
    # Выключает только JSON false; негодный порт -- предупреждение и выключено, без падения.
    caplog.set_level(logging.WARNING, logger="srp.config")
    assert ServerConfig(internet_view=iv).internet_view_port() == port
    assert ("srp.config" in {r.name for r in caplog.records}) is warned


def test_quoted_port_in_config_still_starts(tmp_path: Path, monkeypatch) -> None:
    # «"port": "8000"» uvicorn.run принимал (getaddrinfo); socket.create_server в main()
    # и сравнение в internet_view_port() -- только int.
    path = tmp_path / "config.json"
    path.write_text('{"port": "8000"}', encoding="utf-8")
    monkeypatch.setenv("SRP_CONFIG_PATH", str(path))
    for env in ("PORT", "SRP_PORT"):
        monkeypatch.delenv(env, raising=False)
    cfg = load_config()
    assert cfg.port == 8000 and cfg.internet_view_port() == 8001


def test_internet_view_port_is_off_in_demo(monkeypatch) -> None:
    from server import limits

    monkeypatch.setattr(limits, "DEMO_MODE", True)
    assert ServerConfig().internet_view_port() is None


def test_caddyfile_template_matches_the_gateway() -> None:
    # Шаблон Caddy в корне -- построчно, без отступов и комментариев: admin API выключен,
    # только 443 (без :80), default_sni = адрес сайта (клиент без SNI за NAT иначе получает
    # обрыв TLS), сертификат Let's Encrypt на IP (profile shortlived, проверка по 443),
    # прокси -- на порт шлюза: по умолчанию и в обоих поставляемых конфигах. Заглушка IP --
    # из документационной сети RFC 5737, не чей-то настоящий адрес.
    root = Path(__file__).resolve().parents[1]
    port = ServerConfig().internet_view_port()
    for shipped in ("server/config.json", "srp_packaging/public/config.public.json"):
        assert load_config(root / shipped).internet_view_port() == port, shipped
    text = (root / "Caddyfile").read_text(encoding="utf-8")
    lines = [s for s in (ln.strip() for ln in text.splitlines()) if s and not s.startswith("#")]
    assert lines == [
        "{",
        "admin off",
        "auto_https disable_redirects",
        "default_sni 203.0.113.10",
        "}",
        "203.0.113.10 {",
        "tls {",
        "issuer acme {",
        "profile shortlived",
        "disable_http_challenge",
        "}",
        "}",
        f"reverse_proxy 127.0.0.1:{port}",
        "}",
    ]


def _free_port() -> int:
    with socket.create_server(("127.0.0.1", 0)) as s:
        return s.getsockname()[1]


def _run_main(tmp_path: Path, monkeypatch, proxy_port: int, started: bool = True) -> dict:
    # main() берёт хост и порты из server_main.app; Server.run подменён -- тест ничего не
    # обслуживает, только смотрит, какие сокеты ему открыли, и закрывает их.
    import uvicorn
    from server import main as server_main

    seen: dict = {}

    def fake_run(self, sockets=None) -> None:
        seen["ips"] = self.config.forwarded_allow_ips
        seen["log"] = (self.config.log_config, self.config.access_log)
        seen["addrs"] = [s.getsockname() for s in sockets]
        for s in sockets:
            s.close()
        self.started = started

    iv = {"proxy_port": proxy_port}
    # Основной хост -- не 127.0.0.1: иначе прокси, открытый на cfg.host, неотличим от
    # прокси на 127.0.0.1. Не 0.0.0.0 (как в боевом конфиге): тест не открывает порт сети.
    app = _app(tmp_path, "s3cret", host="127.0.0.2", port=0, internet_view=iv)
    monkeypatch.setattr(server_main, "app", app)
    monkeypatch.setattr(server_main, "_disable_quickedit", lambda: None)
    monkeypatch.setattr(uvicorn.Server, "run", fake_run)
    # Окружение доверяет всем -- main() всё равно принимает X-Forwarded-For только от 127.0.0.1.
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    server_main.main()
    return seen


def test_main_trusts_forwarded_for_only_from_localhost(tmp_path: Path, monkeypatch) -> None:
    from uvicorn.config import LOGGING_CONFIG

    port = _free_port()
    monkeypatch.delenv("SRP_LOG_FILE", raising=False)
    seen = _run_main(tmp_path, monkeypatch, port)
    assert seen["ips"] == "127.0.0.1"
    # Окно start_server.bat (без SRP_LOG_FILE) -- журнал uvicorn и запросов как раньше.
    assert seen["log"] == (LOGGING_CONFIG, True)
    # Два сокета: основной на cfg.host (здесь 127.0.0.2:эфемерный) и прокси Caddy только на
    # 127.0.0.1:port при любом cfg.host -- на 0.0.0.0 он открыл бы http-порт всей сети.
    assert seen["addrs"][0][0] == "127.0.0.2"
    assert seen["addrs"][1] == ("127.0.0.1", port)


def test_main_busy_proxy_port_keeps_the_main_port(tmp_path: Path, monkeypatch, caplog) -> None:
    with socket.create_server(("127.0.0.1", 0)) as busy:
        port = busy.getsockname()[1]
        seen = _run_main(tmp_path, monkeypatch, port)
    assert len(seen["addrs"]) == 1
    assert f"127.0.0.1:{port}" in caplog.text


def test_main_exits_3_when_startup_failed(tmp_path: Path, monkeypatch) -> None:
    # Упавший lifespan (например, init_db): Server.run вернулся без started -- как uvicorn.run.
    with pytest.raises(SystemExit) as exc:
        _run_main(tmp_path, monkeypatch, _free_port(), started=False)
    assert exc.value.code == 3


def test_main_with_srp_log_file_writes_root_log_to_file(tmp_path: Path, monkeypatch) -> None:
    # Под треем окна нет: SRP_LOG_FILE -- корневой журнал INFO в файл (дата, уровень, имя;
    # каталог создаётся), uvicorn свою настройку не ставит, журнал запросов выключен
    # (иначе каждый опрос трея -- строка в файле).
    log = tmp_path / "logs" / "server.log"
    monkeypatch.setenv("SRP_LOG_FILE", str(log))
    root = logging.getLogger()
    level, handlers = root.level, root.handlers[:]
    try:
        seen = _run_main(tmp_path, monkeypatch, _free_port())
        logging.getLogger("srp.retention").info("свип: удалено 0 строк")
    finally:  # корневой логгер общий для всех тестов -- вернуть как было
        for h in [h for h in root.handlers if h not in handlers]:
            root.removeHandler(h)
            h.close()
        root.setLevel(level)
    assert seen["log"] == (None, False)
    line = r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} INFO srp\.retention: свип: удалено 0 строк$"
    assert re.search(line, log.read_text(encoding="utf-8"), re.M)


# --------------------------------------------------------------------------- #
# Порт прокси Caddy (8001): вход по паролю, только чтение (server/internet_view.py)
# --------------------------------------------------------------------------- #
_INET = "https://testserver:8001"  # Caddy -> 127.0.0.1:proxy_port (8001 по умолчанию)
_ORIGIN = {"Origin": _INET}
_STATIC = "/static/plotly-2.35.2.min.js"
_CLOSED = (
    *_AGENT_PATHS,
    "/api/v1/health",
    "/docs",
    "/docs/oauth2-redirect",
    "/openapi.json",
    "/redoc",
    "/deploy",
)


@pytest.fixture
def inet(make_client) -> TestClient:
    """Клиент из интернета (за Caddy) и запись «иванов» с паролем pw."""
    c = make_client("203.0.113.7", base_url=_INET, follow_redirects=False)
    assert db.create_user("иванов", users.hash_password("pw"))
    return c


def _login(c: TestClient, name="иванов", password="pw", query="?next=%2F", headers=_ORIGIN):
    return c.post("/login" + query, data={"username": name, "password": password}, headers=headers)


@pytest.mark.parametrize("host", ("203.0.113.7", "127.0.0.1", "192.168.1.5"))
def test_proxy_port_decides_before_the_address(make_client, host: str) -> None:
    # За Caddy все приходят с 127.0.0.1: «внутренний» адрес на 8001 сессию не заменяет.
    resp = make_client(host, base_url=_INET, follow_redirects=False).get("/")
    assert resp.status_code == 303 and resp.headers["location"] == "/login?next=%2F"


@pytest.mark.parametrize("method", ("GET", "HEAD"))
@pytest.mark.parametrize(
    ("path", "status"), (("/fleet/fragment", 303), (_STATIC, 303), ("/api/v1/devices", 401))
)
def test_without_a_session_pages_go_to_login_and_api_is_401(inet, method, path, status) -> None:
    assert inet.request(method, path).status_code == status


def test_next_keeps_the_whole_query_there_and_back(inet) -> None:
    # Склейка без urlencode отрезала бы &b=2 в отдельный параметр /login.
    loc = inet.get("/print?a=1&b=2").headers["location"]
    assert loc == "/login?" + urlencode({"next": "/print?a=1&b=2"})
    assert _login(inet, query=loc[len("/login") :]).headers["location"] == "/print?a=1&b=2"


def test_session_opens_reading(inet) -> None:
    assert _login(inet).status_code == 303
    assert inet.get("/fleet/fragment").status_code == 200
    assert inet.get("/api/v1/devices").status_code == 200
    assert inet.head(_STATIC).status_code == 200
    # HEAD страницы -- 405 самого FastAPI (APIRoute не знает HEAD): шлюз пропустил.
    assert inet.head("/").status_code == 405


@pytest.mark.parametrize("logged_in", (False, True))
@pytest.mark.parametrize("method", ("POST", "PATCH", "PUT", "DELETE"))
def test_any_change_is_refused_with_or_without_a_session(inet, method, logged_in) -> None:
    # Управление -- только из офиса: PATCH /meta сам по себе без авторизации.
    if logged_in:
        _login(inet)
    assert inet.request(method, "/api/v1/devices/x/meta", json={"comment": "x"}).status_code == 403


def test_closed_paths_are_bare_404_even_with_a_session(inet) -> None:
    _login(inet)
    assert [p for p in _CLOSED if not _filtered(_hit(inet, p, headers=_TOKEN))] == []
    assert all(internet_view.is_closed(p) for p in _CLOSED)  # одна проверка -- и для сита ПДн


def test_plain_http_on_the_proxy_port_is_403(inet) -> None:
    # Без X-Forwarded-Proto: https от Caddy cookie Secure не вернулась бы.
    assert inet.get("http://testserver:8001/login").status_code == 403


def test_websocket_on_the_proxy_port_is_closed(inet) -> None:
    with pytest.raises(WebSocketDisconnect), inet.websocket_connect("wss://testserver:8001/x"):
        pass


def test_proxy_responses_carry_the_security_headers(inet) -> None:
    for resp in (inet.get("/"), inet.get("/deploy"), inet.get("/login")):
        assert resp.headers["x-frame-options"] == "DENY"
        assert resp.headers["content-security-policy"] == "frame-ancestors 'none'"
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers["referrer-policy"] == "same-origin"
        assert resp.headers["cache-control"] == "no-store"
    _login(inet)
    static = inet.head(_STATIC)
    # Plotly (1,4 МБ) без ПДн кэшируется, как в сети: no-store качал бы его на каждой странице.
    assert static.headers["x-frame-options"] == "DENY" and "cache-control" not in static.headers


def test_login_sets_a_strict_session_cookie(inet) -> None:
    resp = _login(inet)
    assert resp.status_code == 303 and resp.headers["location"] == "/"
    cookie = resp.headers["set-cookie"]
    for part in ("jspace_session=", "HttpOnly", "Max-Age=2592000", "Path=/", "SameSite=lax"):
        assert part in cookie
    assert "Secure" in cookie


def test_failed_login_is_the_same_for_every_reason(inet) -> None:
    assert db.create_user("петров", users.hash_password("pw"))
    db.set_user_disabled(db.get_user("петров")["id"], True)
    tries = (_login(inet, password="nope"), _login(inet, name="никто"), _login(inet, name="петров"))
    assert len({(r.status_code, r.text) for r in tries}) == 1
    assert tries[0].status_code == 200 and "Неверное имя или пароль" in tries[0].text
    assert not any("set-cookie" in r.headers for r in tries)


@pytest.mark.parametrize(
    ("headers", "status"),
    (
        ({"Origin": "https://evil.example"}, 403),
        ({"Origin": "null"}, 403),
        ({}, 403),
        ({"Referer": "https://evil.example/login"}, 403),
        ({"Origin": "http://testserver:8001"}, 403),  # схема -- часть origin
        ({"Referer": _INET + ".evil.example/login"}, 403),
        ({"Referer": "http://["}, 403),  # битый Referer -- отказ, а не 500
        ({"Referer": _INET + "/login?next=%2F"}, 303),  # без Origin -- по Referer
    ),
)
def test_login_post_requires_the_same_origin(inet, headers, status: int) -> None:
    assert _login(inet, headers=headers).status_code == status


def test_eleventh_login_a_minute_from_one_address_is_429(inet) -> None:
    codes = [_login(inet, password="nope").status_code for _ in range(11)]
    assert codes == [200] * 10 + [429]
    assert int(_login(inet).headers["retry-after"]) >= 1  # и верный пароль ждёт окна


def test_login_while_another_scrypt_runs_is_429(inet) -> None:
    with internet_view._LOGIN_BUSY:
        resp = _login(inet)
    assert resp.status_code == 429 and resp.headers["retry-after"] == "1"
    assert _login(inet).status_code == 303  # замок отпущен


def test_login_rate_key_is_the_ipv4_or_the_ipv6_64() -> None:
    key = internet_view._rate_key
    assert key("2001:db8:1:2::1") == key("2001:db8:1:2:ffff::9") != key("2001:db8:1:3::1")
    assert key("::ffff:203.0.113.7") == key("203.0.113.7") != key("203.0.113.8")


@pytest.mark.parametrize("target", ("//evil", "/\\evil", "/\t/evil.com", "https://evil", "evil"))
def test_login_redirects_only_to_a_local_path(inet, target: str) -> None:
    assert _login(inet, query="?" + urlencode({"next": target})).headers["location"] == "/"


def test_login_body_over_4kb_is_413(inet) -> None:
    body = urlencode({"username": "иванов", "password": "p" * 4096})
    headers = {**_ORIGIN, "Content-Type": "application/x-www-form-urlencoded"}
    assert inet.post("/login", content=body, headers=headers).status_code == 413


def test_logout_ends_the_session_on_the_server(inet) -> None:
    token = _login(inet).cookies["jspace_session"]
    assert inet.post("/logout", headers={"Origin": "https://evil.example"}).status_code == 403
    assert inet.get("/").status_code == 200  # чужой Origin сессию не тронул
    resp = inet.post("/logout", headers=_ORIGIN)
    assert resp.status_code == 303 and resp.headers["location"] == "/login"
    assert "Max-Age=0" in resp.headers["set-cookie"]
    inet.cookies.clear()
    # Погашена сама сессия в базе, а не только cookie браузера.
    assert inet.get("/", headers={"Cookie": f"jspace_session={token}"}).status_code == 303


def test_login_and_logout_do_not_exist_on_the_main_port(make_client) -> None:
    c = make_client("192.168.1.5")
    assert c.get("/login").status_code == 404 and c.post("/logout").status_code == 404


def test_session_cookie_opens_nothing_on_the_main_port(inet) -> None:
    token = _login(inet).cookies["jspace_session"]
    resp = inet.get("http://testserver:8000/", headers={"Cookie": f"jspace_session={token}"})
    assert _filtered(resp)  # внешний адрес на 8000 -- как прежде, только пути агента


def test_anonymous_login_form_has_no_lan_token_banner(make_client) -> None:
    # Баннер «ingest_token не задан» -- для оператора в сети, не для всего интернета.
    form = make_client("203.0.113.7", token="", base_url=_INET).get("/login")
    assert form.status_code == 200 and 'name="password"' in form.text
    assert "ingest_token не задан" not in form.text and "Неверное" not in form.text
    assert "ingest_token не задан" in make_client("192.168.1.5", token="").get("/").text


def test_login_journal_has_ip_and_known_name_but_no_secrets(inet, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="srp.auth")
    _login(inet, password="Wrong-Pa55")
    _login(inet, name="Typed-Pa55-Into-Name", password="x")  # неизвестное имя -> «-»
    token = _login(inet).cookies["jspace_session"]
    lines = [r.getMessage() for r in caplog.records if "вход из интернета" in r.getMessage()]
    assert len(lines) == 3 and all("203.0.113.7" in line for line in lines)
    assert sum("иванов" in line for line in lines) == 2
    assert not any("Pa55" in line or token in line for line in lines)


def test_viewer_flag_outside_a_request_means_internet() -> None:
    # Потерянный контекст (вне запроса) -- лишняя чистка, а не утечка.
    assert internet_view.is_internet()


# Кнопки: disabled с той же подсказкой, что у стенда; число -- мест на странице с одним ПК
# (fleet.html -- «Очистить офлайн…», _fleet_body.html -- квитировать и удалить).
_READ_ONLY_PAGES = (("/", 3), ("/printers", 1), ("/netmap", 1), ("/device/pc-ro", 0))


def _seed_pc(c: TestClient) -> None:
    for msg_type in ("inventory", "heartbeat"):
        body = envelope("pc-ro", msg_type, healthy(msg_type))
        assert c.post("/api/v1/ingest", json=body, headers=_TOKEN).status_code == 200


def test_internet_pages_are_read_only(inet) -> None:
    lan = TestClient(inet.app)  # тот же сервер из офиса: кнопки и правка -- как прежде
    _seed_pc(lan)
    assert 'action="/logout"' not in inet.get("/login").text  # аноним: выходить некому
    _login(inet)
    for path, buttons in _READ_ONLY_PAGES:
        page, office = inet.get(path).text, lan.get(path).text
        assert page.count('disabled title="только чтение"') == buttons, path
        assert "только чтение" not in office, path
        # Обёртка fetch: 303 на /login или 401 опроса -> перезагрузка, а не форма в #fleet.
        assert "r.redirected" in page and "r.redirected" not in office, path
        assert 'href="/deploy"' not in page and 'href="/deploy"' in office, path
        assert "иванов" in page and 'action="/logout"' in page, path
        assert 'action="/logout"' not in office, path
    device, office = inet.get("/device/pc-ro").text, lan.get("/device/pc-ro").text
    edit = ("comment-input", "owner-save", '"/meta"')
    assert [k for k in edit if k in device] == [] and all(k in office for k in edit)


def test_demo_keeps_its_read_only_text(make_client, monkeypatch) -> None:
    from server import limits

    monkeypatch.setattr(limits, "DEMO_MODE", True)
    page = make_client("192.168.1.5").get("/").text
    assert 'disabled title="демо: только чтение"' in page and "r.redirected" not in page


def test_internet_csv_export_has_its_own_rate_window(inet, make_client, monkeypatch) -> None:
    # Зритель с сессией не запирает выгрузку CSV офису: у интернета своё окно частоты.
    from server import ingest_guards

    monkeypatch.setattr(ingest_guards, "_RATE_MAX_PER_WINDOW", 1)
    assert _login(inet).status_code == 303
    url = "/api/v1/fleet/print/export.csv"
    assert inet.get(url).status_code == 200
    assert inet.get(url).status_code == 429
    assert make_client("192.168.1.5").get(url).status_code == 200
