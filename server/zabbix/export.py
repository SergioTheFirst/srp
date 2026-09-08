"""Один цикл экспорта вердикта в Zabbix.

Ничего не бросает наружу и ничего не пишет в базу: недоступный Zabbix — это факт
о связи, а не о состоянии парка. Собственный неблокирующий лок (не общий
``_poll_lock`` netdisco), иначе отправка встала бы в очередь со сканированием сети.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from server import db
from server.zabbix import items as items_mod
from server.zabbix import sender
from server.zabbix.config import ZabbixConfig
from server.zabbix.protocol import Item
from server.zabbix.status import BackoffStore, StatusStore

log = logging.getLogger("srp.zabbix")

#: Внутренние константы, сознательно НЕ вынесенные в конфигурацию оператора:
#: крутить их незачем, а сломать легко.
DIAGNOSE_COOLDOWN_SEC = 3600.0  # разбор частичного отказа — не чаще раза в час
DIAGNOSE_HOST_CAP = 50  # узлов за один проход разбора
BACKOFF_SEC = 6 * 3600.0  # отсрочка узла, неизвестного Zabbix
DIAGNOSE_BUDGET_SEC = 120.0  # общий потолок времени на один разбор
# Минимальный интервал для кнопки «Отправить сейчас». Эндпоинт неаутентифицирован,
# как и весь дашборд, а один прогон -- это запрос по всей таблице scores плюс залп
# наружу; общего rate-limit (30/мин) для этого мало.
FORCE_MIN_INTERVAL_SEC = 30.0

status = StatusStore()
backoff = BackoffStore(BACKOFF_SEC)

_lock = threading.Lock()
_last_diagnose_at = 0.0
_last_force_at = 0.0

_REJECT_REASONS = (
    "узла нет; узел отключён; имя отличается регистром или доменом; "
    "адрес SRP не разрешён в поле «Allowed hosts» элементов"
)
_REJECT_KEY_REASONS = "элемента нет; элемент отключён; шаблон не привязан"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _send(cfg: ZabbixConfig, batch) -> sender.SendResult:
    return sender.send(batch, host=cfg.server, port=cfg.port, timeout_sec=cfg.timeout_sec)


def _duration(seconds: float) -> str:
    """Длительность по-русски: «45 с», «12 мин», «2 ч 05 мин»."""
    if seconds < 60:
        return f"{int(seconds)} с"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} мин"
    return f"{minutes // 60} ч {minutes % 60:02d} мин"


def _log_outcome(cfg: ZabbixConfig, result: sender.SendResult, packet, elapsed: float) -> None:
    target = f"{cfg.server}:{cfg.port}"
    if result.error:
        if status.should_log_error(result.error_kind or "error"):
            attempts = status.failed_attempts()
            outage = status.outage_seconds() or 0.0
            suffix = (
                f" (не работает {_duration(outage)}, попыток {attempts})" if attempts > 1 else ""
            )
            log.warning("%s%s", result.error, suffix)
        return
    recovered = status.clear_error()
    if recovered is not None:
        # Восстановление печатается ВСЕГДА: «в какой момент связь вернулась» —
        # первый вопрос при разборе инцидента, а подавления тут быть не должно.
        log.info("связь с Zabbix восстановлена, не работала %s", _duration(recovered))
    # Успех подавляется так же, как повтор ошибки: спокойно работающий экспорт
    # писал бы 288 одинаковых строк в сутки. Изменились числа — печатаем сразу.
    signature = f"{result.total}/{result.processed}/{len(packet.hosts)}/{len(packet.skipped)}"
    if not status.should_log_success(signature):
        return
    log.info(
        "отправлено %d значений по %d машинам в Zabbix %s, принято %d, пропущено %d, за %.2f с",
        result.total,
        len(packet.hosts),
        target,
        result.processed,
        len(packet.skipped),
        elapsed,
    )


def _diagnose(cfg: ZabbixConfig, packet) -> list:
    """Найти узлы и ключи, которые Zabbix не принял. Возвращает имена узлов.

    Проход А шлёт по одному значению ``srp.state`` на узел (а не весь набор):
    так у исправных машин в истории Zabbix появляется максимум одна лишняя точка,
    а не десять. Проход Б уточняет ключ у тех, кто прошёл А, но всё равно дал отказ.
    """
    global _last_diagnose_at
    now = time.monotonic()
    if now - _last_diagnose_at < DIAGNOSE_COOLDOWN_SEC:
        return []
    _last_diagnose_at = now

    unknown_hosts: list = []
    # Общий бюджет разбора: до 50 узлов, у каждого свой таймаут -- без потолка
    # медленный собеседник растянул бы разбор на десятки минут и держал бы поток.
    budget_deadline = now + DIAGNOSE_BUDGET_SEC
    hosts = [h for h, _d in packet.hosts][:DIAGNOSE_HOST_CAP]
    if len(packet.hosts) > DIAGNOSE_HOST_CAP:
        log.warning(
            "разбор ограничен %d узлами из %d — остальные проверю в следующий раз",
            DIAGNOSE_HOST_CAP,
            len(packet.hosts),
        )
    for host in hosts:
        if time.monotonic() > budget_deadline:
            log.warning("разбор прерван по бюджету времени — продолжу в следующий раз")
            break
        probe = [Item(host, "srp.state", "unknown")]
        result = _send(cfg, probe)
        if result.error:
            break  # связь пропала прямо во время разбора — дальше смысла нет
        if result.failed:
            unknown_hosts.append(host)
            log.warning(
                "узел «%s» Zabbix не принял ни одного значения. Возможные причины: %s",
                host,
                _REJECT_REASONS,
            )
    _diagnose_keys(cfg, packet, set(unknown_hosts), budget_deadline)
    if unknown_hosts:
        backoff.add(unknown_hosts)
        log.warning(
            "узлы отложены на %d ч; после исправления имени нажмите «Отправить сейчас»",
            int(BACKOFF_SEC // 3600),
        )
    return unknown_hosts


def _diagnose_keys(cfg: ZabbixConfig, packet, unknown_hosts: set, budget: float) -> None:
    """Проход Б: узел Zabbix знает, но какой-то ключ он не принимает."""
    good = [h for h, _d in packet.hosts if h not in unknown_hosts][:1]
    if not good:
        return
    host = good[0]
    keys = sorted({i.key for i in packet.items if i.host == host})
    for key in keys:
        if time.monotonic() > budget:
            return
        result = _send(cfg, [Item(host, key, "0")])
        if result.error:
            return
        if result.failed:
            log.warning(
                "узел «%s», ключ «%s»: значение не принято. Возможные причины: %s",
                host,
                key,
                _REJECT_KEY_REASONS,
            )


def _send_heartbeat(cfg: ZabbixConfig) -> None:
    """Единственный служебный ключ наружу: время последнего расчёта SRP.

    Счётчики отправлено/принято/отвергнуто/пропущено НАРУЖУ не уходят — это
    данные о самом SRP, а не вывод о парке; их видно в SRP на странице
    /pipeline. Zabbix достаточно знать, что SRP жив (штатный nodata).
    """
    beat = items_mod.heartbeat_items(cfg)
    if beat:
        _send(cfg, beat)


def _log_skipped(packet) -> None:
    for skip in packet.skipped[:20]:
        log.warning(
            "машина %s пропущена: %s%s",
            skip.device_id,
            skip.label,
            f" (имя «{skip.host}»)" if skip.host else "",
        )


_announced: Optional[str] = None


def _force_allowed() -> bool:
    """Не чаще раза в FORCE_MIN_INTERVAL_SEC — иначе кнопкой можно молотить парк."""
    global _last_force_at
    now = time.monotonic()
    if now - _last_force_at < FORCE_MIN_INTERVAL_SEC:
        return False
    _last_force_at = now
    return True


def _announce(cfg: ZabbixConfig) -> None:
    """Одна строка при первом запуске и при каждой смене адреса — не каждый цикл."""
    global _announced
    key = f"{cfg.server}:{cfg.port}" if cfg.enabled else ""
    if key == _announced:
        return
    _announced = key
    if not cfg.enabled:
        log.info(
            "экспорт в Zabbix не настроен (server/config.json -> zabbix.server пуст) — "
            "отправка не выполняется"
        )
        return
    log.info("экспорт в Zabbix настроен: %s, интервал %d с", key, cfg.interval_sec)
    warning = cfg.encryption_warning()
    if warning:
        log.warning("%s", warning)


def run_export_cycle(cfg: ZabbixConfig, *, force: bool = False) -> dict:
    """Собрать вердикт по парку и отправить. Никогда не бросает наружу."""
    _announce(cfg)
    if not cfg.enabled:
        status.set_configured(configured=False, target="")
        return {"skipped": "not_configured"}
    status.set_configured(configured=True, target=f"{cfg.server}:{cfg.port}")
    if force and not _force_allowed():
        # Отличается от busy: там цикл реально идёт, здесь просто слишком часто.
        return {"throttled": True}
    if not _lock.acquire(blocking=False):
        return {"busy": True}
    try:
        if force:
            backoff.clear()
        return _run(cfg)
    finally:
        _lock.release()


def _run(cfg: ZabbixConfig) -> dict:
    started = time.monotonic()
    rows = db.get_fleet_verdicts()
    packet = items_mod.build_packet(rows, cfg, backoff_hosts=backoff.active())
    _log_skipped(packet)
    result = _send(cfg, packet.items)
    elapsed = time.monotonic() - started
    _log_outcome(cfg, result, packet, elapsed)

    rejected_hosts: list = []
    if result.ok and result.failed:
        log.warning("Zabbix принял %d из %d значений, разбираюсь", result.processed, result.total)
        rejected_hosts = _diagnose(cfg, packet)
    if result.ok:
        _send_heartbeat(cfg)

    status.record_cycle(
        last_attempt_at=_now_iso(),
        last_success_at=_now_iso() if result.ok else status.snapshot()["last_success_at"],
        next_attempt_at="",
        devices=len(packet.hosts),
        sent=result.total,
        accepted=result.processed,
        rejected=result.failed,
        skipped=len(packet.skipped),
        unknown=packet.unknown,
        last_error=result.error or "",
        last_error_kind=result.error_kind or "",
        rejected_hosts=rejected_hosts or status.snapshot()["rejected_hosts"],
        backoff_hosts=sorted(backoff.active()),
    )
    return {
        "devices": len(packet.hosts),
        "sent": result.total,
        "accepted": result.processed,
        "rejected": result.failed,
        "skipped": len(packet.skipped),
        "unknown": packet.unknown,
        "error": result.error,
    }


def reset_for_tests(cooldown_at: Optional[float] = None) -> None:
    """Сбросить модульное состояние между тестами (отсрочки, окно разбора, анонс)."""
    global _last_diagnose_at, _announced, _last_force_at
    _last_diagnose_at = cooldown_at if cooldown_at is not None else 0.0
    _last_force_at = 0.0
    _announced = None
    backoff.clear()
    status.reset()
