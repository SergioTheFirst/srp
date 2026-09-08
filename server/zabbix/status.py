"""Состояние экспорта в Zabbix в памяти процесса.

По образцу ``server/netdisco/metrics.py``: снимок под ``Lock``, переживать
перезапуск не обязан. История значений ``srp.export.*`` живёт в самом Zabbix —
там она полнее и старше; карточка дашборда честно подписана «с момента запуска».

Здесь же — подавление шума в журнале. Недоступный Zabbix при интервале 300 с
писал бы одну и ту же строку 288 раз в сутки и топил бы всё остальное. Правило:
первая ошибка и смена причины печатаются полностью, повторы — не чаще раза в час,
восстановление печатается всегда.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

_REPEAT_LOG_INTERVAL_SEC = 3600.0


@dataclass
class ExportStatus:
    """Что показывать оператору и чем отвечает /api/v1/zabbix/status."""

    configured: bool = False
    target: str = ""
    started_at: str = ""
    last_attempt_at: str = ""
    last_success_at: str = ""
    next_attempt_at: str = ""
    devices: int = 0
    sent: int = 0
    accepted: int = 0
    rejected: int = 0
    skipped: int = 0
    unknown: int = 0
    last_error: str = ""
    last_error_kind: str = ""
    failing_since: str = ""
    rejected_hosts: list = field(default_factory=list)  # из последнего разбора
    backoff_hosts: list = field(default_factory=list)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StatusStore:
    """Потокобезопасный снимок + решение «печатать ли эту ошибку в журнал»."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status = ExportStatus(started_at=_now_iso())
        self._last_logged_kind: Optional[str] = None
        self._last_logged_at = 0.0
        self._failing_since_mono: Optional[float] = None
        self._failed_attempts = 0
        self._last_success_sig: Optional[str] = None
        self._last_success_log_at = 0.0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "configured": self._status.configured,
                "target": self._status.target,
                "started_at": self._status.started_at,
                "last_attempt_at": self._status.last_attempt_at,
                "last_success_at": self._status.last_success_at,
                "next_attempt_at": self._status.next_attempt_at,
                "devices": self._status.devices,
                "sent": self._status.sent,
                "accepted": self._status.accepted,
                "rejected": self._status.rejected,
                "skipped": self._status.skipped,
                "unknown": self._status.unknown,
                "last_error": self._status.last_error,
                "last_error_kind": self._status.last_error_kind,
                "failing_since": self._status.failing_since,
                "rejected_hosts": list(self._status.rejected_hosts),
                "backoff_hosts": list(self._status.backoff_hosts),
            }

    def set_configured(self, *, configured: bool, target: str) -> None:
        with self._lock:
            self._status.configured = configured
            self._status.target = target

    def record_cycle(self, **fields) -> None:
        with self._lock:
            for name, value in fields.items():
                setattr(self._status, name, value)

    # -- подавление шума ---------------------------------------------------- #
    def should_log_error(self, kind: str) -> bool:
        """Печатать ли эту ошибку сейчас: первая, смена причины или раз в час."""
        now = time.monotonic()
        with self._lock:
            if self._failing_since_mono is None:
                self._failing_since_mono = now
                self._status.failing_since = _now_iso()
            self._failed_attempts += 1
            first_or_changed = kind != self._last_logged_kind
            due = (now - self._last_logged_at) >= _REPEAT_LOG_INTERVAL_SEC
            if first_or_changed or due:
                self._last_logged_kind = kind
                self._last_logged_at = now
                return True
            return False

    def outage_seconds(self) -> Optional[float]:
        with self._lock:
            if self._failing_since_mono is None:
                return None
            return time.monotonic() - self._failing_since_mono

    def failed_attempts(self) -> int:
        with self._lock:
            return self._failed_attempts

    def should_log_success(self, signature: str) -> bool:
        """Успех печатается при первом успехе, при изменении чисел и раз в час.

        Иначе спокойно работающий экспорт писал бы 288 одинаковых строк в сутки
        и заслонял бы в журнале всё остальное.
        """
        now = time.monotonic()
        with self._lock:
            changed = signature != self._last_success_sig
            due = (now - self._last_success_log_at) >= _REPEAT_LOG_INTERVAL_SEC
            if changed or due:
                self._last_success_sig = signature
                self._last_success_log_at = now
                return True
            return False

    def reset(self) -> None:
        """Полный сброс (тесты): снимок, окна подавления и счётчик попыток."""
        with self._lock:
            self._status = ExportStatus(started_at=_now_iso())
            self._last_logged_kind = None
            self._last_logged_at = 0.0
            self._failing_since_mono = None
            self._failed_attempts = 0
            self._last_success_sig = None
            self._last_success_log_at = 0.0

    def clear_error(self) -> Optional[float]:
        """Отметить успех. Возвращает длительность сбоя, если он был."""
        with self._lock:
            outage = None
            if self._failing_since_mono is not None:
                outage = time.monotonic() - self._failing_since_mono
            self._failing_since_mono = None
            self._failed_attempts = 0
            self._last_logged_kind = None
            self._last_logged_at = 0.0
            self._status.last_error = ""
            self._status.last_error_kind = ""
            self._status.failing_since = ""
            return outage


class BackoffStore:
    """Узлы, которые Zabbix отверг целиком, — временно не отправляем.

    Без отсрочки ``srp.export.failed`` навсегда остаётся больше нуля, и триггер
    на него становится бесполезным шумом. Кнопка «Отправить сейчас» сбрасывает
    отсрочку: исправил имя узла — нажал — увидел результат сразу.
    """

    def __init__(self, ttl_sec: float) -> None:
        self.ttl_sec = ttl_sec
        self._lock = threading.Lock()
        self._until: dict = {}

    def active(self) -> set:
        now = time.monotonic()
        with self._lock:
            self._until = {h: t for h, t in self._until.items() if t > now}
            return set(self._until)

    def add(self, hosts) -> None:
        deadline = time.monotonic() + self.ttl_sec
        with self._lock:
            for host in hosts:
                self._until[host] = deadline

    def clear(self) -> None:
        with self._lock:
            self._until.clear()
