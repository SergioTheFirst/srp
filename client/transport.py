"""Transport: deliver envelopes to the server, buffer to disk when offline.

Pure stdlib (urllib) -- the agent ships with zero third-party dependencies, so
it drops onto a domain PC without a pip install. Envelopes that cannot be sent
(server down, network blip, 5xx) are appended to a JSONL buffer and replayed
FIFO on the next successful contact. A payload the server *rejects* (HTTP 4xx)
is dropped, not buffered: retrying a poison message forever would wedge the
queue behind it.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import logging
import os
import random
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from client.config import ClientConfig
from client.winflags import AGENT_ARCH

log = logging.getLogger(__name__)

# Stamped onto every envelope; root VERSION carries the same number (test_version_pin).
AGENT_VERSION = "1.1.0"
# The contract the agent speaks (shared.schema.CONTRACT_VERSION; the agent is stdlib-only
# and cannot import it -- a test pins the two). The server's 406 gate reads it, so the
# agent's own version may have its own MAJOR.
CONTRACT_VERSION = "0.1.0"

_MAX_BUFFER_LINES = 5000  # oldest dropped past this -- bound disk use
# Строковый кап не ограничивает БАЙТЫ: 5000 крупных конвертов -- это гигабайты
# на диске машины пользователя. Второй кап режет самые старые строки по размеру.
_MAX_BUFFER_BYTES = 50 * 1024 * 1024
_SEND_ATTEMPTS = 2  # quick in-process retries before buffering
_RETRY_BACKOFF_SEC = 1.0
# Extra random jitter added to retry sleep to prevent thundering herd when many
# agents reconnect simultaneously after a server outage.
_RETRY_JITTER_SEC = 2.0
# Чуть ниже серверного лимита 512 KiB (server/main.py body-cap): негабарит
# режем ещё на агенте -- не жечь сеть ради гарантированного 413.
_MAX_PAYLOAD_BYTES = 500_000
# Верхняя граница на паузу, которую сервер может попросить через Retry-After:
# кривой/враждебный заголовок не должен усыпить агента надолго.
_MAX_THROTTLE_SEC = 300.0
_DEFAULT_THROTTLE_SEC = 60.0  # столько же, сколько окно server/ingest_guards


def _major(version: Any) -> Optional[int]:
    """MAJOR of a strict MAJOR.MINOR.PATCH string as the ingest gate reads it, else None
    (shared.schema.parse_version; duplicated -- the agent is stdlib-only)."""
    parts = version.split(".") if isinstance(version, str) else []
    strict = len(parts) == 3 and all(p.isascii() and p.isdigit() for p in parts)
    return int(parts[0]) if strict else None


class Transport:
    """Stateless-ish sender bound to one client config."""

    def __init__(self, cfg: ClientConfig) -> None:
        self._cfg = cfg
        self._ingest_url = cfg.server_url.rstrip("/") + "/api/v1/ingest"
        self._buffer = cfg.resolved_buffer_path()
        # Last delivery outcome, surfaced in status.json for the tray panel.
        self.last_ok_ts: Optional[float] = None
        self.last_error: str = ""
        self._down = ""  # причина обрыва (сеть/5xx) -- только для «связь восстановлена»
        # До этого момента сеть не трогаем: сервер уже сказал «слишком часто».
        self._throttled_until: float = 0.0

    def buffer_depth(self) -> int:
        """Сколько конвертов ждёт в офлайн-буфере (потоково, без чтения файла целиком)."""
        if not self._buffer.exists():
            return 0
        try:
            with self._buffer.open("r", encoding="utf-8", errors="replace") as fh:
                return sum(1 for line in fh if line.strip())
        except OSError as exc:
            log.error("could not read buffer: %s", exc)
            return 0

    # -- public API -------------------------------------------------------- #
    def send(
        self,
        msg_type: str,
        payload: Optional[dict[str, Any]],
        source_health: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Build and deliver an envelope. Buffer it on transient failure.

        Returns True if the envelope was delivered (or permanently rejected),
        False if it was buffered for a later retry (liveness: never buffered --
        the server stamps last_seen on receipt, a replayed ping says nothing,
        and pings were 91 % of the buffer's lines). An envelope with no payload
        is still sent when it carries source_health (so the server learns a
        source is down); a fully empty send is a no-op.
        """
        if payload is None and not source_health:
            log.debug("skipping %s: no payload and no source health", msg_type)
            return True
        envelope = self._envelope(msg_type, payload or {}, source_health)
        # Проверка размера ДО доставки: негабарит сервер отвергнет гарантированно,
        # а в офлайне он вообще не доходил до _attempt и оседал в буфере навсегда.
        size = len(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))
        if size > _MAX_PAYLOAD_BYTES:
            self.last_error = f"payload {size} bytes > cap"
            log.warning(
                "oversized %s payload (%d bytes) -- dropping, not buffering", msg_type, size
            )
            return True  # обработан = отброшен
        if self._deliver(envelope):
            self.flush_buffer()  # server is reachable -- drain backlog too
            return True
        if msg_type != "liveness":
            self._append_buffer(envelope)
        return False

    def flush_buffer(self) -> int:
        """Replay buffered envelopes oldest-first.

        Stops at the first transient failure, keeping that envelope and every
        later one for next time. Returns how many were cleared (sent/dropped).
        """
        lines = self._read_buffer()
        if not lines:
            return 0
        handled = 0
        foreign = 0
        remaining: list[str] = []
        blocked = False
        for line in lines:
            if blocked:
                remaining.append(line)
                continue
            try:
                envelope = json.loads(line)
            except json.JSONDecodeError:
                log.warning("dropping corrupt buffer line")
                handled += 1
                continue
            if envelope.get("msg_type") == "liveness":
                handled += 1  # pings buffered by 0.3.3: purged, never POSTed
                continue
            if envelope.get("device_id") != self._cfg.device_id:
                # 0.3.5: конверт прежнего device_id (сброс клона, потеря config.json)
                # досылать нельзя -- он «оживит» старую запись поверх новой
                # (spec 2026-10-24-canonical-devices). Данные мёртвой личности не нужны.
                foreign += 1
                handled += 1
                continue
            if self._deliver(envelope):
                handled += 1
            else:
                blocked = True
                remaining.append(line)
        if foreign:  # одна строка, а не по строке на конверт (буфер до 5000 строк)
            log.warning("dropped %d buffered envelope(s) of a previous device_id", foreign)
        if handled:  # nothing sent = file unchanged -- no rewrite every minute (F4)
            self._write_buffer(remaining)
            log.info("flushed %d buffered envelope(s), %d remaining", handled, len(remaining))
        return handled

    # -- delivery ---------------------------------------------------------- #
    def _envelope(
        self, msg_type: str, payload: dict[str, Any], source_health: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        return {
            "device_id": self._cfg.device_id,
            # Live machine name on every envelope -> the dashboard shows the real
            # name on first contact of any type, not only on the rare inventory.
            # Empty -> None so the server's COALESCE keeps any stored name.
            "hostname": self._cfg.hostname or None,
            "agent_version": AGENT_VERSION,
            "contract_version": CONTRACT_VERSION,
            "agent_arch": AGENT_ARCH,  # 0.3.6: x86-агента дашборд сверяет с x86-пакетом
            "msg_type": msg_type,
            "ts": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
            "source_health": source_health or {},
            # Send None when empty so the server's COALESCE keeps any existing value.
            "site_code": self._cfg.site_code or None,
            "site_name": self._cfg.site_name or None,
            "org_code": self._cfg.org_code or None,
            "dept_code": self._cfg.dept_code or None,
            "comment": self._cfg.comment or None,
            # Персональные данные владельца ПК (F10): пустая строка -> None, чтобы
            # серверный COALESCE сохранил уже записанное значение.
            "owner_full_name": self._cfg.owner_full_name or None,
            "owner_position": self._cfg.owner_position or None,
            "owner_phone": self._cfg.owner_phone or None,
            # P1: client-generated idempotency key lets the server dedup retried
            # envelopes. UUID4 hex = 32 chars, stable for the lifetime of this
            # envelope object (buffered replays reuse the same key).
            "idempotency_key": uuid.uuid4().hex,
        }

    def _deliver(self, envelope: dict[str, Any]) -> bool:
        """True if handled (delivered or 4xx-rejected); False if it should buffer."""
        if self._cfg.offline_mode:
            # P1-1: never attempt a network call in offline mode -- server_url may
            # be empty, and urlopen() raises ValueError (not caught below) on a
            # schemeless relative URL, crashing the caller. Buffer instead; a later
            # run with offline_mode off and a real server_url flushes the backlog.
            return False
        if time.time() < self._throttled_until:
            # Окно лимитера ещё закрыто -- запрос гарантированно получит 429.
            # Молча буферизуем: в журнале сервера это была серия обречённых 429
            # на КАЖДЫЙ тип сообщения цикла.
            return False
        for attempt in range(1, _SEND_ATTEMPTS + 1):
            outcome = self._attempt(envelope)
            if outcome in ("ok", "drop"):
                return True
            if outcome == "throttled":
                return False  # ретрай через 1-3 с при окне в минуту обречён
            if attempt < _SEND_ATTEMPTS:
                # Jitter prevents thundering herd when many agents reconnect at once.
                time.sleep(_RETRY_BACKOFF_SEC + random.uniform(0.0, _RETRY_JITTER_SEC))  # nosec B311 -- timing jitter is not a security primitive
        return False

    def _attempt(self, envelope: dict[str, Any]) -> str:
        """One POST. Returns 'ok' | 'drop' | 'retry'."""
        # sent_ts = момент ЭТОЙ попытки: по нему сервер меряет дрейф часов (ts -- время
        # сбора, у повтора из буфера оно старое). Строка буфера остаётся исходной.
        sent = {**envelope, "sent_ts": datetime.now(timezone.utc).isoformat()}
        body = json.dumps(sent, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._cfg.ingest_token:
            headers["X-SRP-Token"] = self._cfg.ingest_token
        req = urllib.request.Request(
            self._ingest_url,
            data=body,
            method="POST",
            headers=headers,
        )
        try:
            # B310: scheme is the operator-configured server_url, not user input.
            with urllib.request.urlopen(req, timeout=self._cfg.http_timeout_sec):  # nosec B310
                if self._down:  # 4xx/429/негабарит -- не обрыв связи
                    log.info("связь восстановлена (было: %s)", self._down)
                    self._down = ""
                self.last_ok_ts = time.time()
                self.last_error = ""
                return "ok"  # urlopen only returns for 2xx/3xx
        except urllib.error.HTTPError as exc:  # subclass of URLError -> catch first
            return self._http_error(exc, envelope)
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
            # HTTPException (BadStatusLine...): urllib не оборачивает ошибки getresponse()
            # -- это не OSError, конверт терялся, а flush ронял цикл агента.
            level = self._record_error(str(exc)[:200])
            self._down = self.last_error
            log.log(level, "network error sending %s: %s", envelope.get("msg_type"), exc)
            return "retry"

    def _http_error(self, exc: urllib.error.HTTPError, envelope: dict[str, Any]) -> str:
        """An HTTP error reply -> 'throttled' | 'drop' | 'retry' (split out of _attempt)."""
        msg_type = envelope.get("msg_type")
        # HTTPError -- это ЕЩЁ И объект ответа. Не дочитав и не закрыв его, мы
        # оставляем сокет на сборщик мусора: сервер видит принудительный обрыв
        # (WinError 10054) и печатает трейсбек в окне оператора.
        with contextlib.suppress(OSError, ValueError, http.client.HTTPException):
            exc.read()
        with contextlib.suppress(OSError, AttributeError):
            exc.close()
        level = self._record_error(f"HTTP {exc.code}")
        spoken = envelope.get("contract_version") or envelope.get("agent_version")
        stale = exc.code == 406 and _major(spoken) != _major(CONTRACT_VERSION)
        if exc.code in (401, 406, 408, 425, 429) and not stale:
            # Сервер тормозит (ingest_guards: 30 конвертов / 60 с на устройство), а не
            # отвергает содержимое: это ровно тот случай, ради которого буфер и есть.
            # 401 туда же (AUTH-4): токен не принят -- состояние сервера, а не плохой
            # конверт; «выбросить» выжигало весь офлайн-буфер одним циклом.
            # 406 туда же, если конверт говорит на MAJOR текущего контракта агента (агент
            # 1.0.0: сервер откатили ниже развязки контракта) -- выбросить = сжечь буфер.
            # Строку другого MAJOR (записана до самообновления; у 0.3.x контракт -- их
            # agent_version, как читает сервер) сервер этого контракта не примет никогда:
            # она «ядовитая» (ниже), иначе клинит голову буфера навсегда. MINOR/PATCH
            # гейт не проверяет (D2) -- такая строка остаётся в буфере вместе с живыми.
            wait = self._retry_after(exc)
            self._throttled_until = time.time() + wait
            log.log(
                level,
                "server throttled %s (HTTP %d) -- buffering, retry in %.0fs",
                msg_type,
                exc.code,
                wait,
            )
            return "throttled"
        if 400 <= exc.code < 500:
            log.log(level, "server rejected %s (HTTP %d) -- dropping", msg_type, exc.code)
            return "drop"
        log.log(level, "server error HTTP %d on %s", exc.code, msg_type)
        self._down = self.last_error
        return "retry"

    def _record_error(self, err: str) -> int:
        """Remember err as last_error; return its log level -- WARNING once per outage,
        DEBUG while the same error repeats (2 attempts + a flush every <=60 s filled
        85 % of the 4 x 1 MB log with one line, rotating out the cause in days)."""
        level = logging.DEBUG if err == self.last_error else logging.WARNING
        self.last_error = err
        return level

    @staticmethod
    def _retry_after(exc: urllib.error.HTTPError) -> float:
        """Сколько ждать по заголовку Retry-After; дефолт -- окно лимитера.

        Значение зажато сверху: заголовок приходит по сети, и «подождите сутки»
        не должен выключать телеметрию машины.
        """
        raw = ""
        headers = getattr(exc, "headers", None)
        if headers is not None:
            try:
                raw = headers.get("Retry-After") or ""
            except AttributeError:
                raw = ""
        try:
            wait = float(str(raw).strip())
        except (TypeError, ValueError):
            return _DEFAULT_THROTTLE_SEC
        if wait <= 0:
            return _DEFAULT_THROTTLE_SEC
        return min(wait, _MAX_THROTTLE_SEC)

    # -- buffer I/O -------------------------------------------------------- #
    def _read_buffer(self) -> list[str]:
        if not self._buffer.exists():
            return []
        try:
            # errors=replace: строка, оборванная посреди UTF-8 символа (BSOD, питание),
            # падает в json.loads и отбрасывается, а не роняет агента (F1).
            lines = self._buffer.read_text(encoding="utf-8", errors="replace").splitlines()
            return [ln for ln in lines if ln.strip()]
        except OSError as exc:
            log.error("could not read buffer: %s", exc)
            return []

    def _write_buffer(self, lines: list[str]) -> None:
        """Перезапись через временный файл + os.replace: сбой на середине записи
        оставляет прежний буфер целым (образец -- client/config.py, tray/spool.py)."""
        try:
            if not lines:
                if self._buffer.exists():
                    self._buffer.unlink()
                return
            tmp = self._buffer.with_suffix(self._buffer.suffix + ".tmp")
            try:
                tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
                os.replace(tmp, self._buffer)
            except OSError:
                # Не оставлять недописанный tmp: он копит до второго капа на диске.
                with contextlib.suppress(OSError):
                    tmp.unlink(missing_ok=True)
                raise
        except OSError as exc:
            log.error("could not rewrite buffer: %s", exc)

    def _append_buffer(self, envelope: dict[str, Any]) -> None:
        try:
            self._buffer.parent.mkdir(parents=True, exist_ok=True)
            with self._buffer.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(envelope, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.error("could not buffer %s: %s", envelope.get("msg_type"), exc)
            return
        self._trim_buffer()

    def _trim_buffer(self) -> None:
        try:  # дешёвая проверка: пересчёт байтов нужен только когда файл реально велик
            oversize = self._buffer.stat().st_size > _MAX_BUFFER_BYTES
        except OSError:
            return
        lines = self._read_buffer()
        kept = lines[-_MAX_BUFFER_LINES:]  # срез копирует: lines остаётся эталоном длины
        if oversize:
            # Текстовый режим переводит "\n" в os.linesep -- считаем реальные байты файла.
            nl = len(os.linesep)
            total = sum(len(ln.encode("utf-8")) + nl for ln in kept)
            dropped = 0
            while kept and total > _MAX_BUFFER_BYTES:  # самые старые уходят первыми
                total -= len(kept[0].encode("utf-8")) + nl
                del kept[0]
                dropped += 1
            if dropped:
                log.warning("buffer over %d bytes -- dropped %d oldest", _MAX_BUFFER_BYTES, dropped)
        if len(kept) != len(lines):
            self._write_buffer(kept)
