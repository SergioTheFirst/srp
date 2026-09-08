"""Локальный приёмник протокола траппера Zabbix для тестов.

Это не заглушка вместо кода, а вторая сторона настоящего обмена: приёмник
реализует протокол целиком (заголовок ZBXD, флаги, длина little-endian, тело
JSON, корректный ответ с числом принятых значений) и сертифицируется официальным
``zabbix_sender`` — см. ``tests/test_zabbix_wire.py``. Наш отправитель затем
проверяется против уже сертифицированного приёмника.

Слушает 127.0.0.1 на порту, который выдаёт ОС; поток-демон; у всех сокетов
таймауты, чтобы зависший тест не подвесил весь прогон на Windows.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import struct
import threading
import time
import zlib
from typing import Callable, Optional

HEADER_LEN = 13
_ACCEPT_TIMEOUT_SEC = 0.25
_CONN_TIMEOUT_SEC = 5.0


def find_zabbix_sender() -> Optional[str]:
    """Путь к официальному zabbix_sender: переменная окружения -> tools/ -> PATH."""
    env = os.environ.get("SRP_ZABBIX_SENDER")
    if env and os.path.isfile(env):
        return env
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("zabbix_sender.exe", "zabbix_sender"):
        candidate = os.path.join(root, "tools", name)
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("zabbix_sender")


SENDER_MISSING_REASON = (
    "нет официального zabbix_sender: положите bin/zabbix_sender.exe из "
    "zabbix_agent-*-windows-amd64.zip в tools/ или задайте SRP_ZABBIX_SENDER"
)


class TrapperStub:
    """Приёмник траппера с задаваемым поведением.

    mode:
        ``ok``       — принять всё;
        ``partial``  — отвергнуть значения, на которых ``reject`` вернул True;
        ``failed``   — ответить ``{"response":"failed"}``;
        ``slow``     — подождать ``delay_sec`` и ответить как ``ok``;
        ``close``    — прочитать пакет и закрыть соединение без ответа;
        ``garbage``  — ответить байтами, не похожими на ZBXD;
        ``oversize`` — объявить в заголовке длину больше нашего потолка.
    """

    def __init__(
        self,
        mode: str = "ok",
        *,
        reject: Optional[Callable[[dict], bool]] = None,
        delay_sec: float = 0.0,
    ) -> None:
        self.mode = mode
        self.reject = reject
        self.delay_sec = delay_sec
        self.packets: list = []  # разобранные тела запросов, по одному на соединение
        self.raw: list = []  # сырые байты запросов
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._sock.settimeout(_ACCEPT_TIMEOUT_SEC)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    # -- жизненный цикл ---------------------------------------------------- #
    def __enter__(self) -> "TrapperStub":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3.0)
        with contextlib.suppress(OSError):
            self._sock.close()

    # -- разбор запроса ---------------------------------------------------- #
    @staticmethod
    def _recv_exactly(conn: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                break
            buf += chunk
        return buf

    def _read_request(self, conn: socket.socket) -> Optional[dict]:
        header = self._recv_exactly(conn, HEADER_LEN)
        if len(header) < HEADER_LEN or header[:4] != b"ZBXD":
            return None
        flags = header[4]
        length, _reserved = struct.unpack("<II", header[5:HEADER_LEN])
        if length > 16 * 1024 * 1024:
            return None
        body = self._recv_exactly(conn, length)
        self.raw.append(header + body)
        if flags & 0x02:
            body = zlib.decompress(body)
        return json.loads(body.decode("utf-8"))

    # -- ответ ------------------------------------------------------------- #
    @staticmethod
    def _frame(obj: dict) -> bytes:
        payload = json.dumps(obj).encode("utf-8")
        return b"ZBXD\x01" + struct.pack("<II", len(payload), 0) + payload

    def _response_for(self, request: dict) -> Optional[bytes]:
        data = request.get("data") or []
        total = len(data)
        failed = 0
        if self.mode == "partial" and self.reject is not None:
            failed = sum(1 for item in data if self.reject(item))
        if self.mode == "failed":
            return self._frame({"response": "failed", "info": "cannot process"})
        if self.mode == "garbage":
            return b"HTTP/1.1 500 Internal Server Error\r\n\r\n"
        if self.mode == "oversize":
            return b"ZBXD\x01" + struct.pack("<II", 64 * 1024 * 1024, 0) + b"{}"
        processed = total - failed
        info = f"processed: {processed}; failed: {failed}; total: {total}; seconds spent: 0.000123"
        return self._frame({"response": "success", "info": info})

    # -- цикл обслуживания ------------------------------------------------- #
    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with conn:
                conn.settimeout(_CONN_TIMEOUT_SEC)
                try:
                    request = self._read_request(conn)
                except (OSError, ValueError, zlib.error):
                    continue
                if request is None:
                    continue
                self.packets.append(request)
                if self.mode == "close":
                    continue
                if self.mode == "slow":
                    time.sleep(self.delay_sec)
                    if self._stop.is_set():
                        continue
                response = self._response_for(request)
                if response:
                    try:
                        conn.sendall(response)
                    except OSError:
                        continue

    # -- удобства для тестов ----------------------------------------------- #
    def values(self) -> list:
        """Все значения из всех принятых пакетов, по порядку."""
        out: list = []
        for packet in self.packets:
            out.extend(packet.get("data") or [])
        return out

    def by_key(self, key: str) -> list:
        return [v["value"] for v in self.values() if v.get("key") == key]

    def wait_for_packets(self, count: int, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.packets) >= count:
                return True
            time.sleep(0.01)
        return len(self.packets) >= count
