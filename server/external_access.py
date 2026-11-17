"""Интернету -- только три пути агента; остальное для него «не существует» (404).

Дашборд не аутентифицирован, а порт сервера проброшен наружу (установка агента через
интернет, docs/superpowers/specs/2026-10-28-internet-install-design.md). Внутренний
клиент -- RFC1918, loopback, link-local (IPv6: ::1, fe80::/10, fc00::/7); всё прочее,
включая CGNAT 100.64/10 и неразборчивый адрес, -- внешний. Внешнему открыты только
пути агента и только с верным X-SRP-Token (пустой ingest_token = снаружи закрыто всё).

Токен внешнего запроса проверяется здесь, до чтения тела: иначе каждый чужой POST
на /ingest стоил бы буферизации и pydantic-разбора до 401 (дешёвый DoS слабого сервера).
Отказ -- тот же голый 404. Прошедший токен считается в окне на IP (держатель токена
мог бы иначе крутить device_id -- лимит /ingest идёт по нему).

Адрес берётся из ``scope["client"]``: за Caddy на этом же хосте uvicorn подставляет
X-Forwarded-For только от 127.0.0.1 (main.py пинает ``forwarded_allow_ips``), поэтому
внешний клиент заголовком «внутренним» не станет. Всегда включено, без флага; кроме
демо-витрины (limits.DEMO_MODE, Vercel): там посетители -- весь интернет, а сама
витрина только для чтения.

Запрос на порт прокси просмотра из интернета (``app.state.internet_port``, за Caddy)
решает server/internet_view.py -- раньше адреса: за Caddy все приходят с 127.0.0.1.
"""

from __future__ import annotations

import hmac
import ipaddress
from typing import Optional

from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.websockets import WebSocketClose

from server import internet_view, limits
from server.ingest_guards import check_rate_limit, count_reject, retry_after_sec

AGENT_PATHS = frozenset({"/api/v1/ingest", "/api/v1/agent/update", "/api/v1/agent/update/package"})

# Запросов в минуту с одного внешнего IP. Офис за NAT -- один адрес на весь парк: ~100 ПК
# x (heartbeat раз в 5 мин + 4-5 сборщиков в час + проверка обновления в час) ~ 30/мин;
# утренний старт (каждый ПК шлёт ~6 конвертов разом) и досылка буфера после ночи без
# связи -- до ~600/мин. Сверху -- 429 с Retry-After, агент дошлёт из буфера. Ключи живут
# в общем словаре ingest_guards (обрезка _TRIM_THRESHOLD): число IP ограничено
# держателями токена, чужие без токена в окно не попадают. Вход с порта прокси (ключи
# login-ip:) добавляет не больше ключа на scrypt -- он один за раз (internet_view).
EXTERNAL_MAX_PER_MINUTE = 600

# Явный список вместо ip.is_private: тот включает 2001::/23 (Teredo -- адрес из
# интернета) и менял состав между версиями Python.
_INTERNAL_NETS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "::1/128",
        "fe80::/10",
        "fc00::/7",
    )
)


def is_internal(host: Optional[str]) -> bool:
    """LAN или этот же ПК; IPv4-mapped IPv6 (::ffff:a.b.c.d) разворачивается."""
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])  # fe80::1%eth0 -> без зоны
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in net for net in _INTERNAL_NETS)


def _token_ok(scope: Scope) -> bool:
    """X-SRP-Token из заголовков scope против ingest_token -- без чтения тела."""
    app = scope.get("app")
    expected = str(getattr(getattr(app, "state", None), "ingest_token", "") or "")
    if not expected:
        return False
    provided = next((v for k, v in scope.get("headers", ()) if k == b"x-srp-token"), b"")
    return hmac.compare_digest(provided, expected.encode("utf-8"))


def _external_refusal(scope: Scope, host: str) -> Optional[Response]:
    """None -- пропустить внешний запрос; иначе ответ отказа."""
    if scope.get("path") not in AGENT_PATHS or not _token_ok(scope):
        return Response(status_code=404)
    key = f"ext:{host}"  # своё пространство ключей, как device:/endpoint: в api.py
    if not check_rate_limit(key, EXTERNAL_MAX_PER_MINUTE):
        count_reject("rate_limit")
        retry = retry_after_sec(key, EXTERNAL_MAX_PER_MINUTE)
        return Response(status_code=429, headers={"Retry-After": str(retry)})
    return None


class ExternalAccessMiddleware:
    """Чистый ASGI: внешнему клиенту вне путей агента или без токена -- голый 404."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):  # у lifespan нет scope["server"]
            await self.app(scope, receive, send)
            return
        # Порт прокси решает раньше адреса: за Caddy все приходят с 127.0.0.1.
        port = scope["app"].state.internet_port
        if port is not None and scope["server"][1] == port:
            await internet_view.gate(self.app, scope, receive, send)
            return
        # Сеть; до выхода демо, иначе стенд Vercel получил бы вид «из интернета».
        internet_view.VIEWER.set(None)
        client = scope.get("client")
        host = client[0] if client else ""
        if limits.DEMO_MODE or is_internal(host):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await WebSocketClose()(scope, receive, send)
            return
        refusal = _external_refusal(scope, host)
        if refusal is not None:
            await refusal(scope, receive, send)
            return
        await self.app(scope, receive, send)
