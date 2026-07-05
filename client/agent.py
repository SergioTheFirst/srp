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
import getpass
import logging
import random
import sys
import time
import urllib.parse
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


class Agent:
    def __init__(self, cfg: ClientConfig) -> None:
        self._cfg = cfg
        self._transport = Transport(cfg)
        self._updater = Updater(cfg)
        state_path = cfg.resolved_buffer_path().with_name("print_state.json")
        self._print_state_path = state_path
        self._tasks: list[tuple[str, Collector, str]] = [
            t for t in TASKS if t[0] != "historical"
        ] + [
            (
                "historical",
                partial(collect_historical, active_scan=cfg.active_scan),
                "historical_interval_sec",
            ),
            (
                "print_jobs",
                partial(collect_print_jobs, state_path, autoenable=cfg.print_log_autoenable),
                "print_interval_sec",
            ),
        ]

    def run_once(self) -> None:
        """Run every collector a single time (used by --once and at startup)."""
        for msg_type, collector, _ in self._tasks:
            self._run_task(msg_type, collector)
        publish_status(self._cfg, self._transport, self._print_state_path)

    def run_forever(self) -> None:
        """Loop forever, running each task when its interval comes due."""
        self._reconcile_update()
        due = {msg_type: time.monotonic() for msg_type, _, _ in self._tasks}  # all due now
        # Anti-thundering-herd: spread the fleet's first update check over 60-600s
        # so a restart of many agents at once does not all hit the server together.
        update_due = time.monotonic() + random.uniform(  # nosec B311 -- timing jitter, not security
            _UPDATE_CHECK_MIN_SEC, _UPDATE_CHECK_MAX_SEC
        )
        try:
            while True:
                for msg_type, collector, interval_attr in self._tasks:
                    if time.monotonic() >= due[msg_type]:
                        self._run_task(msg_type, collector)
                        interval = max(1, int(getattr(self._cfg, interval_attr)))
                        due[msg_type] = time.monotonic() + interval
                if time.monotonic() >= update_due:
                    if self._run_update_check():
                        break  # restart pending -- let the update task take over
                    interval = max(
                        _UPDATE_CHECK_MIN_INTERVAL_SEC, int(self._cfg.update_check_interval_sec)
                    )
                    update_due = time.monotonic() + interval
                # Retry any backlog even when no task is due (no-op if buffer empty).
                self._transport.flush_buffer()
                # Refresh the tray's one-way status file every loop iteration.
                publish_status(self._cfg, self._transport, self._print_state_path)
                sleep_for = min([*due.values(), update_due]) - time.monotonic()
                time.sleep(max(1.0, min(sleep_for, _MAX_SLEEP_SEC)))
        except KeyboardInterrupt:
            log.info("interrupted -- shutting down")

    def _run_task(self, msg_type: str, collector: Collector) -> None:
        try:
            result = collector()
        except Exception:  # noqa: BLE001 -- a broken collector must not kill the loop
            log.exception("collector %s raised", msg_type)
            return
        if result.payload is None and not result.source_health:
            log.warning("%s: collector produced nothing (source blocked?)", msg_type)
            return
        delivered = self._transport.send(msg_type, result.payload, result.source_health)
        log.info("%s: %s", msg_type, "sent" if delivered else "buffered (offline)")

    def _reconcile_update(self) -> None:
        """Report the outcome of an update staged before this restart, if any."""
        try:
            rec = self._updater.reconcile_after_restart(AGENT_VERSION)
        except Exception:  # noqa: BLE001 -- a broken updater must not kill the loop
            log.exception("update reconcile raised")
            return
        if rec:
            self._transport.send("update_status", rec)

    def _run_update_check(self) -> bool:
        """Run one update check/apply cycle. Returns True if a restart is pending."""
        try:
            payload, restart = self._updater.check(AGENT_VERSION)
        except Exception:  # noqa: BLE001 -- a broken updater must not kill the loop
            log.exception("update check raised")
            return False
        if payload:
            self._transport.send("update_status", payload)
        if restart:
            log.info("update staged -- exiting so the update task can take over")
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


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv)
    setup_logging(args.verbose, args.log_file)
    cfg = load_config()
    if args.server:
        cfg.server_url = args.server
    try:
        validate_runtime_config(cfg)
    except ConfigError as exc:
        log.error("%s", exc)
        raise SystemExit(2) from exc
    log.info(
        "SRP agent starting -- user=%s device_id=%s server=%s",
        getpass.getuser(),
        cfg.device_id,
        _redacted_url(cfg.server_url),
    )

    agent = Agent(cfg)
    if args.once:
        agent.run_once()
        log.info("single pass complete")
    else:
        agent.run_forever()


if __name__ == "__main__":
    main()
