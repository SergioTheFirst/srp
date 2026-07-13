"""W4.0: развязка ingest и scoring -- коалесцирующий фоновый rescore-воркер.

Ingest сохраняет телеметрию синхронно (durability не меняется) и лишь ставит
устройство в очередь; один daemon-поток разгребает её, так что шторм конвертов
одного устройства стоит один пересчёт, а медленный recompute больше не сидит
внутри HTTP-запроса. Sync-режим (async_rescore=false, дефолт кода) сохраняет
сегодняшнее поведение: пересчёт инлайн, свежие скоры в ответе ingest. Shipped
server/config.json включает async (правило no-dormant): ответ тогда несёт
scores=null -- все потребители это уже переживают (агент поле не читает,
дашборд поллит /api/v1/devices).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

_JOIN_TIMEOUT_SEC = 5.0


class RescoreQueue:
    """Коалесцирующий per-device rescore-воркер (in-process, один поток).

    ponytail: одного потока и set-коалесценции хватает до сотен устройств;
    выделенный воркер-процесс -- когда drain на живом флоте станет заметен.
    """

    def __init__(self, recompute: Callable[[str], object]) -> None:
        self._recompute = recompute
        self._pending: set[str] = set()
        self._cond = threading.Condition()
        self._busy = 0
        self._stopping = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="rescore-queue", daemon=True)
        self._thread.start()

    def submit(self, device_id: str) -> None:
        with self._cond:
            if self._stopping:
                return
            self._pending.add(device_id)
            self._cond.notify()

    def drain(self, timeout_sec: float = 10.0) -> bool:
        """Ждать, пока очередь пуста и воркер простаивает (тесты/шатдаун)."""
        deadline = time.monotonic() + timeout_sec
        with self._cond:
            while self._pending or self._busy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
        return True

    def stop(self) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(_JOIN_TIMEOUT_SEC)

    def _loop(self) -> None:
        while True:
            with self._cond:
                while not self._pending and not self._stopping:
                    self._cond.wait()
                if self._stopping and not self._pending:
                    return
                device_id = self._pending.pop()
                self._busy += 1
            try:
                self._recompute(device_id)
            except Exception:  # одно битое устройство не должно убить воркер
                log.exception("background rescore failed for %s", device_id)
            finally:
                with self._cond:
                    self._busy -= 1
                    self._cond.notify_all()
