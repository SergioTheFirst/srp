"""Шлюз порта просмотра из интернета: вход по паролю, только чтение.

Спека docs/superpowers/specs/2026-11-05-dashboard-login-design.md §2, §5-6. Caddy на
этом же ПК принимает https и передаёт запрос на 127.0.0.1:<internet_view.proxy_port>;
по порту (``scope["server"]``) server/external_access.py отдаёт запрос сюда раньше
любой проверки адреса. Порядок:

- websocket -- закрыт; не https (нет X-Forwarded-Proto от Caddy) -- 403;
- пути агента, /api/v1/health, /docs, /openapi.json, /redoc, /deploy -- голый 404;
- /login (GET, HEAD, POST) и /logout (POST) -- здесь же: роутера нет, на 8000 их нет;
- любой метод, кроме GET/HEAD, -- 403 с любой сессией: управление только из офиса;
- прочее -- только с сессией (cookie -> SHA-256 -> sessions); без неё /api/* -- 401,
  остальное -- 303 на /login?next=<путь?запрос>.

ПДн зрителю отсюда не уходят: redact() -- в каждом рендере (dashboard._Templates) и в
ответах API (demo_redact.for_public); сырые блобы агента срезает /api/v1/devices/{id}.
"""

from __future__ import annotations

import ipaddress
import logging
import threading
from contextvars import ContextVar
from typing import Any, Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlsplit

from shared.schema import utcnow_iso
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from server import db, users
from server.ingest_guards import check_rate_limit, retry_after_sec

_authlog = logging.getLogger("srp.auth")

# None -- запрос из сети (8000), "" -- аноним с 8001, имя -- с сессией. Умолчания нет:
# не заданный (потерянный контекст, вне запроса) читается как «интернет» -- лишняя
# чистка, а не утечка. ExternalAccessMiddleware перезаписывает его на каждом запросе.
VIEWER: ContextVar[Optional[str]] = ContextVar("internet_viewer")

_COOKIE = "jspace_session"
_FORM_MAX_BYTES = 4096
_LOGIN_PER_MINUTE = 10  # с IPv4-адреса или IPv6 /64; ключа по имени нет (спека §6)
# ponytail: один scrypt (~40 мс, 16 МиБ) на все входы из интернета; наплыв задерживает
# только новые входы -- сессии, сеть и агенты его не замечают.
_LOGIN_BUSY = threading.Lock()

# С 8001 -- голый 404: агенты ходят на 8000, схема API и /api/v1/health наружу не нужны,
# а команды установки с /deploy указывали бы на 8001. Эту же проверку берёт сито ПДн.
_CLOSED = frozenset({"/api/v1/health", "/openapi.json", "/redoc", "/deploy"})

_HEADERS = (
    ("X-Frame-Options", "DENY"),
    ("Content-Security-Policy", "frame-ancestors 'none'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "same-origin"),
)

# Ключи ПДн: владелец ПК и комментарий (агента и оператора -- db уже слил *_operator в те
# же ключи), заметка «принято», владелец, субъект и издатель сертификата (личного и
# машинного: в КЭП -- ФИО; у самоподписанного издатель = субъект), пользователь печати,
# текст события (C:\Users\<логин>\), секрет удаления.
_PII = frozenset(
    [
        "owner_full_name",
        "owner_position",
        "owner_phone",
        "comment",
        "note",
        "owner",
        "subject",
        "issuer",
        "user_name",
        "message",
        "admin_token",
    ]
)
# Целиком в []: график печати по людям («Нет данных»), задания IPP (документ -- под общим
# ключом name), сырые захваты mDNS/SSDP в base64.
_EMPTIED = frozenset({"users", "ipp_jobs", "lan_hints"})


def is_internet() -> bool:
    """Запрос с порта прокси -- или флаг не задан (тоже «интернет»)."""
    return VIEWER.get("") is not None


def is_closed(path: str) -> bool:
    from server.external_access import AGENT_PATHS  # внутри: external_access импортирует нас

    return path in AGENT_PATHS or path in _CLOSED or path.startswith("/docs")


def redact(obj: Any) -> Any:
    """Копия без ПДн для зрителя из интернета (спека §3); вход не меняется.

    Обход dict, list и кортежей: парк на странице -- список кортежей (объект, [ПК]),
    dashboard._group_by_site; NamedTuple собирается своим типом. Прочее -- как есть."""
    if isinstance(obj, dict):
        return {k: [] if k in _EMPTIED else redact(v) for k, v in obj.items() if k not in _PII}
    if isinstance(obj, list):
        return [redact(item) for item in obj]
    if isinstance(obj, tuple):
        items = [redact(item) for item in obj]
        return type(obj)(*items) if hasattr(obj, "_fields") else tuple(items)
    return obj


async def gate(app: ASGIApp, scope: Scope, receive: Receive, send: Send) -> None:
    """Каждый http/websocket-запрос с порта прокси (сопоставление -- по scope["path"])."""
    VIEWER.set("")
    if scope["type"] == "websocket":
        await WebSocketClose()(scope, receive, send)
        return
    path, method = scope["path"], scope["method"]
    send = _secured(send, path)
    if scope.get("scheme") != "https":
        response: Response = Response(status_code=403)
    elif is_closed(path):
        response = Response(status_code=404)
    elif path == "/login" and method in ("GET", "HEAD", "POST"):
        response = await _login(Request(scope, receive))
    elif path == "/logout" and method == "POST":
        response = await _logout(Request(scope, receive))
    elif method not in ("GET", "HEAD"):
        response = Response(status_code=403)
    else:
        name = await _session_user(scope)
        if name is not None:
            VIEWER.set(name)
            await app(scope, receive, send)
            return
        response = _to_login(scope, path)
    await response(scope, receive, send)


def _secured(send: Send, path: str) -> Send:
    """Заголовки на каждый ответ шлюза; no-store -- где нет своего Cache-Control, кроме
    /static/ (Plotly 1,4 МБ без ПДн кэшируется, как в сети). 500 от ServerErrorMiddleware
    (он снаружи) идёт без них -- там простой текст."""

    async def wrapped(message: Message) -> None:
        if message["type"] == "http.response.start":
            headers = MutableHeaders(scope=message)
            for key, value in _HEADERS:
                headers[key] = value
            if not path.startswith("/static/"):
                headers.setdefault("Cache-Control", "no-store")
        await send(message)

    return wrapped


async def _session_user(scope: Scope) -> Optional[str]:
    token = Request(scope).cookies.get(_COOKIE)
    if not token:
        return None
    return await run_in_threadpool(db.get_session_user, users.token_sha256(token))


def _to_login(scope: Scope, path: str) -> Response:
    if path.startswith("/api/"):
        return Response(status_code=401)
    query = scope.get("query_string", b"").decode("latin-1")
    # urlencode целиком: склейка «next=/print?a=1&b=2» отрезала бы &b=2.
    target = path + ("?" + query if query else "")
    return RedirectResponse("/login?" + urlencode({"next": target}), status_code=303)


def _same_origin(request: Request) -> bool:
    """Origin (без него -- Referer) = scheme://Host; нет обоих или Origin: null -- чужой.

    Сравнение строк, без разбора URL: битый Referer («http://[») -- отказ, а не 500."""
    host = request.headers.get("host")
    if not host:
        return False
    expected = f"{request.scope['scheme']}://{host}"
    origin = request.headers.get("origin")
    if origin is not None:
        return origin == expected
    referer = request.headers.get("referer", "")
    return referer == expected or referer.startswith(expected + "/")


async def _read_form(request: Request) -> Optional[dict[str, list[str]]]:
    """Тело не больше 4 КБ (иначе None) -> parse_qs: python-multipart не в зависимостях."""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > _FORM_MAX_BYTES:
            return None
    return parse_qs(body.decode("latin-1"))


async def _login(request: Request) -> Response:
    if request.method != "POST":
        return _form(request)
    if not _same_origin(request):
        return Response(status_code=403)
    form = await _read_form(request)
    if form is None:
        return Response(status_code=413)
    if not _LOGIN_BUSY.acquire(blocking=False):
        return Response(status_code=429, headers={"Retry-After": "1"})
    try:
        name, password = (form.get(k, [""])[0] for k in ("username", "password"))
        return await _attempt(request, name, password)
    finally:
        _LOGIN_BUSY.release()


async def _attempt(request: Request, name: str, password: str) -> Response:
    """Одна попытка входа под _LOGIN_BUSY: окно IP, scrypt в пуле потоков, журнал."""
    ip = request.client.host if request.client else ""
    key = _rate_key(ip)
    if not check_rate_limit(key, _LOGIN_PER_MINUTE):
        retry = retry_after_sec(key, _LOGIN_PER_MINUTE)
        return Response(status_code=429, headers={"Retry-After": str(retry)})
    token, known = await run_in_threadpool(_check, name, password)
    # Журнал -- окно start_server.bat, под треем -- logs\server.log на диске (ротация --
    # main._file_logging); пароль, cookie и токен не пишутся никогда.
    _authlog.warning(
        "%s вход из интернета: %s, ip=%s, запись=%s",
        utcnow_iso(),
        "успешно" if token else "отказ",
        ip,
        known,
    )
    if token is None:
        return _form(request, failed=True)
    target = _safe_next(request.query_params.get("next", ""))
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(
        _COOKIE,
        token,
        max_age=db.SESSION_DAYS * 86400,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    return response


def _check(name: str, password: str) -> Tuple[Optional[str], str]:
    """users.login и имя записи для журнала -- одним вызовом из пула потоков. Имя --
    только существующей записи: в поле имени мог оказаться пароль."""
    token = users.login(name, password)
    user = db.get_user(users._norm(name))
    return token, user["username"] if user else "-"


def _rate_key(ip: str) -> str:
    """IPv4 -- сам адрес, IPv6 -- его /64: у одного абонента IPv6-адресов миллионы."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return f"login-ip:{ip}"
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is None:
            return f"login-ip:{ipaddress.ip_network(f'{addr}/64', strict=False)}"
        addr = addr.ipv4_mapped  # ::ffff:a.b.c.d -- тот же IPv4, не общий /64 всех IPv4
    return f"login-ip:{addr}"


def _safe_next(target: str) -> str:
    """Только локальный путь (спека §2), иначе /: //host, /\\host и /<TAB>/host браузер
    открыл бы как чужой сайт."""
    if (
        target.startswith("/")
        and not target.startswith("//")
        and "\\" not in target
        and all(ch > " " for ch in target)
    ):
        parts = urlsplit(target)
        if not parts.scheme and not parts.netloc:
            return target
    return "/"


def _form(request: Request, failed: bool = False) -> Response:
    from server.web.dashboard import _TEMPLATES  # local: dashboard импортирует этот модуль

    return _TEMPLATES.TemplateResponse(request, "login.html", {"failed": failed})


async def _logout(request: Request) -> Response:
    if not _same_origin(request):
        return Response(status_code=403)
    token = request.cookies.get(_COOKIE)
    if token:
        await run_in_threadpool(db.delete_session, users.token_sha256(token))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response
