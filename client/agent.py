"""SRP agent: the main loop that collects telemetry and ships it to the server.

Each of the four message types runs on its own cadence (heartbeat often, the
inventory/historical scans rarely) so a busy office PC is barely touched. The
loop is deliberately dumb: collect -> send -> reschedule. All the analysis lives
on the server; the agent only reports what it observed.

Run it::

    python -m client.agent            # forever, on the configured intervals
    python -m client.agent --once     # one pass of every collector, then exit
    python -m client.agent --server http://10.0.0.5:8000   # override target
"""

from __future__ import annotations

import argparse
import ctypes
import getpass
import logging
import random
import sys
import time
import urllib.parse
from ctypes import wintypes
from functools import partial
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable, Optional

from client.collectors import (
    collect_events,
    collect_heartbeat,
    collect_historical,
    collect_inventory,
    collect_liveness,
)
from client.collectors.print_jobs import collect_print_jobs
from client.collectors.sources import CollectorResult
from client.collectors.user_identity import collect_owner_identity
from client.config import ClientConfig, ConfigError, load_config, validate_runtime_config
from client.status_writer import publish_status
from client.transport import AGENT_VERSION, Transport
from client.updater import Updater

log = logging.getLogger("srp.agent")

Collector = Callable[[], CollectorResult]

# (msg_type, collector, name-of-interval-field-on-the-config)
TASKS: list[tuple[str, Collector, str]] = [
    ("inventory", collect_inventory, "inventory_interval_sec"),
    ("historical", collect_historical, "historical_interval_sec"),
    ("heartbeat", collect_heartbeat, "heartbeat_interval_sec"),
    ("events", collect_events, "events_interval_sec"),
    # Частый «я жив» без телеметрии: сервер лишь обновляет last_seen -> offline
    # на дашборде виден за ~2 пропущенных пинга, а не за 4-часовой цикл.
    ("liveness", collect_liveness, "liveness_interval_sec"),
]

_MAX_SLEEP_SEC = 60.0  # cap so a buffered backlog gets retried even when idle
_UPDATE_CHECK_MIN_SEC = 60.0  # first update check no sooner than this...
_UPDATE_CHECK_MAX_SEC = 600.0  # ...and no later than this (anti-thundering-herd)
_UPDATE_CHECK_MIN_INTERVAL_SEC = 300  # floor for repeat checks even if config sets less
# Как часто перечитывать трей-спул персональных данных владельца ПК.
_OWNER_REFRESH_SEC = 60.0
# Сбой inventory (холодный WMI в первые минуты после загрузки: timeout/empty) ->
# повтор через 10 мин, затем 20, 40... но не реже штатного интервала.
_INVENTORY_RETRY_SEC = 600
# setup.exe гасит агента сам (schtasks /end + taskkill) через секунды после
# постановки задачи обновления; пережили это окно -- задача не стартовала.
_HANDOFF_WAIT_SEC = 300
# После старта и пробуждения ПК всё, кроме liveness, ждёт 1-5 мин: утром на входе
# пользователя холодный WMI валил inventory на 7 из 28 ПК (спека 2026-10-14).
_SETTLE_MIN_SEC = 60.0
_SETTLE_MAX_SEC = 300.0
# agent 1.0.0 (H16): the heartbeat waits for the historical (its SMART script takes <= 60 s)
_HIST_FIRST_GAP_SEC = 60.0
# Сон цикла затянулся сильнее этого = ПК спал/гибернировался: time.monotonic
# (GetTickCount64) идёт и во сне, а Fast Startup сохраняет процесс агента.
_WAKE_GAP_SEC = 300.0


def _settle(due: dict[str, float]) -> None:
    """Push every task but liveness 1-5 min out; the heartbeat goes after the historical
    (H16: the morning's first heartbeat recompute then sees fresh SMART, not a stale
    source); the update check keeps its own jitter."""
    delays = {
        m: random.uniform(_SETTLE_MIN_SEC, _SETTLE_MAX_SEC)  # nosec B311 -- timing jitter
        for m in due
        if m != "liveness"
    }
    if "historical" in delays and "heartbeat" in delays:
        delays["heartbeat"] = max(delays["heartbeat"], delays["historical"] + _HIST_FIRST_GAP_SEC)
    now = time.monotonic()
    for msg_type, delay in delays.items():
        due[msg_type] = max(due[msg_type], now + delay)


def _log_sent(msg_type: str, delivered: bool, result: CollectorResult) -> None:
    """Строка отправки + не-ok источники «src=status[:reason]» (агент 0.3.5): причина
    видна в srp-agent.log без гаданий по пустому {}. WARNING -- сбор упал (payload None)
    или есть причина сбоя PowerShell; законная пустота -- INFO."""
    bad = {src: h for src, h in sorted(result.source_health.items()) if h.get("status") != "ok"}
    detail = " ".join(
        f"{src}={h.get('status')}" + (f":{h['reason']}" if h.get("reason") else "")
        for src, h in bad.items()
    )
    failing = result.payload is None or any(h.get("reason") for h in bad.values())
    log.log(
        logging.WARNING if failing else logging.INFO,
        "%s: %s%s",
        msg_type,
        "sent" if delivered else "buffered (offline)",
        f" [{detail}]" if detail else "",
    )


class Agent:
    def __init__(self, cfg: ClientConfig) -> None:
        self._cfg = cfg
        self._owner_read_at = 0.0  # см. _refresh_owner_identity
        self._inventory_retry_sec = _INVENTORY_RETRY_SEC  # см. _inventory_delay
        self._transport = Transport(cfg)
        self._updater = Updater(cfg)
        state_path = cfg.resolved_buffer_path().with_name("print_state.json")
        self._print_state_path = state_path
        historical = partial(collect_historical, active_scan=cfg.active_scan)
        # TASKS order: the historical is checked before the heartbeat, so a pass that runs
        # past the settle gap (inventory timeout, slow send) still sends SMART first (H16).
        self._tasks: list[tuple[str, Collector, str]] = [
            (m, historical, a) if m == "historical" else (m, c, a) for m, c, a in TASKS
        ] + [
            (
                "print_jobs",
                partial(collect_print_jobs, state_path, autoenable=cfg.print_log_autoenable),
                "print_interval_sec",
            ),
        ]

    def run_once(self) -> bool:
        """Run every collector a single time (--once: the installer's link check).

        True = the server accepted something: a 2xx, or a 429 (api.py checks the
        token before the rate limit), or offline_mode (no link to check).
        """
        for msg_type, collector, _ in self._tasks:
            self._run_task(msg_type, collector)
        publish_status(self._cfg, self._transport, self._print_state_path)
        t = self._transport
        if t.last_ok_ts is not None or t.last_error == "HTTP 429" or self._cfg.offline_mode:
            return True
        log.error("проверка связи: сервер не принял ни одного конверта (%s)", t.last_error)
        return False

    def run_forever(self) -> None:
        """Loop forever, running each task when its interval comes due."""
        self._reconcile_update()
        due = {msg_type: time.monotonic() for msg_type, _, _ in self._tasks}
        _settle(due)  # liveness now, the heavy collectors 1-5 min after start
        # Anti-thundering-herd: spread the fleet's first update check over 60-600s
        # so a restart of many agents at once does not all hit the server together.
        update_due = time.monotonic() + random.uniform(  # nosec B311 -- timing jitter, not security
            _UPDATE_CHECK_MIN_SEC, _UPDATE_CHECK_MAX_SEC
        )
        try:
            while True:
                try:
                    update_due = self._tick(due, update_due)
                except Exception:  # noqa: BLE001 -- one bad pass must not kill the agent (RG-2)
                    log.exception("agent loop pass raised")
                    time.sleep(_MAX_SLEEP_SEC)
        except KeyboardInterrupt:
            log.info("interrupted -- shutting down")

    def _tick(self, due: dict[str, float], update_due: float) -> float:
        """One loop pass: backlog, due tasks, update check, status, sleep.

        Returns the next update-check time.
        """
        # Backlog FIRST (F3): a returning network drains older envelopes before
        # this pass collects -- a buffered failed inventory can't land after the
        # fresh retry and flip identity back. Guarded on its own: a broken flush
        # must not skip the collectors.
        try:
            self._transport.flush_buffer()
        except Exception:  # noqa: BLE001
            log.exception("buffer flush raised")
        for msg_type, collector, interval_attr in self._tasks:
            if time.monotonic() >= due[msg_type]:
                ok = self._run_task(msg_type, collector)
                interval = max(1, int(getattr(self._cfg, interval_attr)))
                if msg_type == "inventory":
                    interval = self._inventory_delay(ok, interval)
                due[msg_type] = time.monotonic() + interval
        if time.monotonic() >= update_due:
            if self._run_update_check():
                self._await_handoff()
            interval = max(_UPDATE_CHECK_MIN_INTERVAL_SEC, int(self._cfg.update_check_interval_sec))
            update_due = time.monotonic() + interval
        # Refresh the tray's one-way status file every loop iteration.
        publish_status(self._cfg, self._transport, self._print_state_path)
        now = time.monotonic()
        nap = max(1.0, min(min([*due.values(), update_due]) - now, _MAX_SLEEP_SEC))
        time.sleep(nap)
        if time.monotonic() - now - nap > _WAKE_GAP_SEC:  # the PC slept: everything is overdue
            _settle(due)
        return update_due

    def _inventory_delay(self, ok: bool, interval: int) -> int:
        """Next inventory run: the normal interval, or a retry after a failed run.

        identity comes only from inventory, so one failed run (cold WMI right
        after boot: timeout/empty) = untrusted for a whole cycle (4 h; 24 h on
        old configs). ponytail: doubling 10 -> 20 -> 40 min capped at the normal
        interval -- a permanently broken PC costs ~5 extra PowerShell runs per
        agent start, then lives on the normal cadence. Success resets the ladder.
        """
        if ok:
            self._inventory_retry_sec = _INVENTORY_RETRY_SEC
            return interval
        delay = min(self._inventory_retry_sec, interval)
        self._inventory_retry_sec = min(self._inventory_retry_sec * 2, interval)
        log.warning("inventory failed -- retrying in %d s", delay)
        return delay

    def _await_handoff(self) -> None:
        """An update is staged: setup.exe stops this process itself within seconds.

        The agent used to exit here -- when the update task never started (AV,
        policy) the PC stayed dark until a REAL reboot (Fast Startup does not
        re-fire the BootTrigger). Idle instead (a kill can't tear a write), then,
        if still alive, report the failed handoff and carry on collecting.
        """
        time.sleep(_HANDOFF_WAIT_SEC)
        log.warning("update task did not take over in %d s -- carrying on", _HANDOFF_WAIT_SEC)
        self._send_update_status(self._updater.handoff_failed())

    def _refresh_owner_identity(self) -> None:
        r"""Подтянуть персональные данные владельца ПК из трей-спула В ПАМЯТЬ.

        Конфиг агент не пишет: ``C:\SRP`` закрыт на запись для Users, а запись
        конфига требует пароль (см. F.0 плана). Значения живут только в
        ``self._cfg`` до следующего перезапуска и уезжают в каждом конверте.

        # ponytail: перечитывание раз в 60 с -- маленький файл, дешевле, чем
        # следить за mtime.
        """
        now = time.time()
        if now - self._owner_read_at < _OWNER_REFRESH_SEC:
            return
        self._owner_read_at = now
        try:
            owner = collect_owner_identity()
        except Exception:  # noqa: BLE001 -- спул пишет НЕадминистратор, вход враждебный
            log.exception("owner identity read raised")
            return
        self._cfg.owner_full_name = owner.get("owner_full_name") or ""
        self._cfg.owner_position = owner.get("owner_position") or ""
        self._cfg.owner_phone = owner.get("owner_phone") or ""

    def _run_task(self, msg_type: str, collector: Collector) -> bool:
        """Collect + send. True = a clean run: payload present, every source "ok",
        envelope handed to the transport (delivered or buffered -- the buffer
        replays it). False = worth re-collecting (drives the inventory retry)."""
        self._refresh_owner_identity()
        try:
            result = collector()
        except Exception:  # noqa: BLE001 -- a broken collector must not kill the loop
            log.exception("collector %s raised", msg_type)
            return False
        if result.payload is None and not result.source_health:
            log.warning("%s: collector produced nothing (source blocked?)", msg_type)
            return False
        try:
            delivered = self._transport.send(msg_type, result.payload, result.source_health)
        except Exception:  # noqa: BLE001 -- a broken send must not kill the loop
            log.exception("send %s raised", msg_type)
            return False
        if msg_type == "liveness":  # every 5 min and never buffered (transport.send)
            log.debug("liveness: %s", "sent" if delivered else "not sent")
        else:
            _log_sent(msg_type, delivered, result)
        return result.payload is not None and all(
            h.get("status") == "ok" for h in result.source_health.values()
        )

    def _send_update_status(self, payload: dict) -> None:
        try:
            self._transport.send("update_status", payload)
        except Exception:  # noqa: BLE001 -- a broken send must not kill the loop
            log.exception("update status send raised")

    def _reconcile_update(self) -> None:
        """Report the outcome of an update staged before this restart, if any."""
        try:
            rec = self._updater.reconcile_after_restart(AGENT_VERSION)
        except Exception:  # noqa: BLE001 -- a broken updater must not kill the loop
            log.exception("update reconcile raised")
            return
        if rec:
            self._send_update_status(rec)

    def _run_update_check(self) -> bool:
        """Run one update check/apply cycle. Returns True if a restart is pending."""
        try:
            payload, restart = self._updater.check(AGENT_VERSION)
        except Exception:  # noqa: BLE001 -- a broken updater must not kill the loop
            log.exception("update check raised")
            return False
        if payload:
            self._send_update_status(payload)
        if restart:
            log.info("update staged -- setup.exe takes over")
        return restart


_LOG_MAX_BYTES = 1_000_000  # ~1 MB per file
_LOG_BACKUPS = 3  # keep srp-agent.log plus .1/.2/.3


def setup_logging(verbose: bool, log_file: Optional[str] = None) -> None:
    """Configure root logging: a console handler when a stream exists, plus an
    optional rotating file.

    A SYSTEM scheduled task has no console, so the install step passes
    ``--log-file`` to capture diagnostics to a bounded, rotating file. A windowed
    (no-console) build has ``sys.stderr is None`` -- there the console handler is
    skipped (it would raise on every emit) and the file handler carries the logs.
    Idempotent: re-running replaces handlers rather than stacking duplicates.
    """
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        handler.close()
        root.removeHandler(handler)
    # A windowed (no-console) PyInstaller build has sys.stderr is None; a plain
    # StreamHandler there raises on every emit. Attach a console handler only when
    # a stream exists -- the rotating file below carries the logs headless.
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        rotating = RotatingFileHandler(
            path, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUPS, encoding="utf-8"
        )
        rotating.setFormatter(fmt)
        root.addHandler(rotating)


def _redacted_url(url: str) -> str:
    """Strip any ``user:pass@`` userinfo from a URL before it is logged."""
    split = urllib.parse.urlsplit(url)
    if "@" in split.netloc:
        split = split._replace(netloc=split.netloc.rsplit("@", 1)[1])
    return urllib.parse.urlunsplit(split)


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="srp-agent", description="SRP telemetry agent")
    parser.add_argument(
        "--once", action="store_true", help="run each collector once and exit (no loop)"
    )
    parser.add_argument(
        "--server", metavar="URL", help="override server_url from config for this run"
    )
    parser.add_argument(
        "--log-file",
        metavar="PATH",
        help="also write logs to this rotating file (used when running as a service)",
    )
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    return parser.parse_args(argv)


# Один демон на машину. Живой инцидент 2026-08-02: работали ДВА srp-agent.exe
# (задача SYSTEM плюс запущенный вручную). Они делят C:\SRPuffer.jsonl, а
# flush_buffer читает файл целиком, отправляет всё и записывает остаток обратно --
# поэтому запись одного процесса ВОСКРЕШАЛА строки, уже доставленные другим.
# Буфер не пустел никогда, сервер получал бесконечный поток повторов.
# Global\, а не Local\: агент штатно живёт задачей SYSTEM (сессия 0), а лишняя
# копия запускается в сессии пользователя -- Local-мьютекс их бы не увидел.
_MUTEX_NAME = r"Global\SRPAgentInstanceMutex"
_ERROR_ALREADY_EXISTS = 183
_ERROR_ACCESS_DENIED = 5


def acquire_single_instance() -> Optional[int]:
    """Хэндл именованного мьютекса; None -- демон уже запущен.

    0 означает «взять мьютекс не удалось» (не Windows, чужой сеанс) -- работаем
    без защиты, потому что отсутствие проверки не повод не собирать телеметрию.
    Мьютекс освобождается ОС при смерти процесса, поэтому убитый агент не
    блокирует следующий запуск.
    """
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k32.CreateMutexW.restype = wintypes.HANDLE
        handle = k32.CreateMutexW(None, False, _MUTEX_NAME)
    except (AttributeError, OSError):
        return 0
    err = ctypes.get_last_error()
    # ACCESS_DENIED на Global-имени трактуем как «уже запущен»: у обычного
    # пользователя нет права создавать глобальные объекты, поэтому такой отказ
    # означает либо «объект есть, доступа нет», либо «эта копия и не должна была
    # запускаться демоном». Оба случая -- повод не стартовать вторым.
    if err in (_ERROR_ALREADY_EXISTS, _ERROR_ACCESS_DENIED):
        return None
    return int(handle) if handle else 0


# Watchdog pause marker (W, 2026-10-15). setup.exe (file swap) and the tray
# «Выход» drop C:\SRP\update\hold so the agent, if relaunched by the 5-minute
# TimeTrigger mid-operation, exits at once instead of fighting for locked files
# or defeating «Выход». Same relative path as client.deploy.update_recovery.HOLD
# (pinned equal by a test); defined here independently because the agent cannot
# import client.deploy -- the public tree ships no client/deploy.
_HOLD = Path("update") / "hold"
_HOLD_SWAP_TTL_SEC = 30 * 60  # a "swap" marker older than this = setup.exe died; ignore it


def _uptime_sec() -> Optional[float]:
    """Seconds since boot via GetTickCount64, or None off Windows / on error.

    Same ctypes pattern as acquire_single_instance (passes mypy on the Linux CI
    job). Lets held() reject a marker left over from a PREVIOUS boot by its mtime.
    """
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.GetTickCount64.restype = ctypes.c_uint64
        return k32.GetTickCount64() / 1000
    except (AttributeError, OSError):
        return None


def held(install_dir: Path, now: Optional[float] = None) -> bool:
    """True if a live pause marker tells this agent not to start.

    A marker counts only if its mtime is after boot -- a leftover from a
    previous boot never holds. A "swap" marker also expires after 30 min (setup
    was killed mid-swap); a "stop" marker (tray «Выход») has no TTL -- it holds
    until the next reboot. Uptime unavailable -> fail-open (better to run than
    stay dark on a quirk).
    """
    marker = install_dir / _HOLD
    try:
        mtime = marker.stat().st_mtime
    except OSError:
        return False
    try:
        content = marker.read_bytes()
    except OSError:
        content = b""
    uptime = _uptime_sec()
    if uptime is None:
        return False  # fail-open: better to run than stay dark
    if now is None:
        now = time.time()
    if mtime < now - uptime:
        return False  # left over from a previous boot
    # No upper bound: a clock stepped back after the marker was written must not
    # release a «Выход» pause; a "swap" marker expires either way after the TTL.
    return content.strip() != b"swap" or abs(now - mtime) < _HOLD_SWAP_TTL_SEC


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv)
    setup_logging(args.verbose, args.log_file)
    if not args.once and getattr(sys, "frozen", False) and held(Path(sys.executable).parent):
        log.info(
            "пауза (update\\hold): setup.exe меняет файлы или «Выход» в трее -- "
            "агент не запускается"
        )
        return
    cfg = load_config()
    if args.server:
        cfg.server_url = args.server
    try:
        validate_runtime_config(cfg)
    except ConfigError as exc:
        log.error("%s", exc)
        raise SystemExit(2) from exc
    log.info(
        "SRP agent starting -- version=%s user=%s device_id=%s server=%s",
        AGENT_VERSION,
        getpass.getuser(),
        cfg.device_id,
        _redacted_url(cfg.server_url),
    )

    agent = Agent(cfg)
    if args.once:
        # --once НЕ берёт мьютекс: этим режимом установщик проверяет связь, и он
        # обязан отработать по-настоящему. Проход короткий, окно пересечения мало.
        if not agent.run_once():
            raise SystemExit(3)  # setup.exe: rc != 0 -> «сервер недоступен», задача не ставится
        log.info("single pass complete")
        return
    if acquire_single_instance() is None:
        log.error(
            "srp-agent уже запущен на этой машине -- второй экземпляр завершается. "
            "Два агента делят один офлайн-буфер и бесконечно пересылают друг другу "
            "уже доставленные конверты."
        )
        raise SystemExit(3)
    agent.run_forever()


if __name__ == "__main__":
    main()
