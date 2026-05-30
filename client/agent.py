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
import logging
import time
from typing import Callable, Optional

from client.collectors import (
    collect_events,
    collect_heartbeat,
    collect_historical,
    collect_inventory,
)
from client.collectors.sources import CollectorResult
from client.config import ClientConfig, load_config
from client.transport import Transport

log = logging.getLogger("srp.agent")

Collector = Callable[[], CollectorResult]

# (msg_type, collector, name-of-interval-field-on-the-config)
TASKS: list[tuple[str, Collector, str]] = [
    ("inventory", collect_inventory, "inventory_interval_sec"),
    ("historical", collect_historical, "historical_interval_sec"),
    ("heartbeat", collect_heartbeat, "heartbeat_interval_sec"),
    ("events", collect_events, "events_interval_sec"),
]

_MAX_SLEEP_SEC = 60.0  # cap so a buffered backlog gets retried even when idle


class Agent:
    def __init__(self, cfg: ClientConfig) -> None:
        self._cfg = cfg
        self._transport = Transport(cfg)

    def run_once(self) -> None:
        """Run every collector a single time (used by --once and at startup)."""
        for msg_type, collector, _ in TASKS:
            self._run_task(msg_type, collector)

    def run_forever(self) -> None:
        """Loop forever, running each task when its interval comes due."""
        due = {msg_type: time.monotonic() for msg_type, _, _ in TASKS}  # all due now
        try:
            while True:
                for msg_type, collector, interval_attr in TASKS:
                    if time.monotonic() >= due[msg_type]:
                        self._run_task(msg_type, collector)
                        interval = max(1, int(getattr(self._cfg, interval_attr)))
                        due[msg_type] = time.monotonic() + interval
                # Retry any backlog even when no task is due (no-op if buffer empty).
                self._transport.flush_buffer()
                sleep_for = min(due.values()) - time.monotonic()
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


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="srp-agent", description="SRP telemetry agent")
    parser.add_argument(
        "--once", action="store_true", help="run each collector once and exit (no loop)"
    )
    parser.add_argument(
        "--server", metavar="URL", help="override server_url from config for this run"
    )
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> None:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    cfg = load_config()
    if args.server:
        cfg.server_url = args.server
    log.info("SRP agent starting -- device_id=%s server=%s", cfg.device_id, cfg.server_url)

    agent = Agent(cfg)
    if args.once:
        agent.run_once()
        log.info("single pass complete")
    else:
        agent.run_forever()


if __name__ == "__main__":
    main()
