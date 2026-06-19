"""Phase 4 — server-side printer poll scheduler.

Probes the discovered candidate list concurrently (a ThreadPoolExecutor over the
blocking SNMP collector) and stores one reading per candidate: a live snapshot
when the host answers, or a synthetic "unreachable" reading when it does not, so
a down printer stays visible in the dashboard instead of vanishing. One bad host
never kills the cycle (device-ghost-cleanup lesson: the guard lives inside the
worker).

This module is server-bound glue (it imports ``server.db``), unlike the rest of
the stdlib-only printers package. It NEVER scans address ranges -- it only probes
hosts already surfaced by silent discovery (spooler hints + ARP + the engineer's
static list). Active range scanning stays behind the phase-7 security stop-gate
(``PrinterConfig.active_scan``), which this scheduler neither consults nor performs.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence

from server import db
from server.printers import collector, discovery
from server.printers.config import PrinterConfig
from server.printers.discovery import PrinterCandidate
from server.printers.models import PrinterReading, printer_identity

_log = logging.getLogger("srp.printers")

_MAX_WORKERS = 16  # bound the SNMP fan-out; printer fleets are small

ProbeFn = Callable[..., Optional[PrinterReading]]
StoreFn = Callable[..., None]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_reading(cand: PrinterCandidate, reading: Optional[PrinterReading]) -> dict[str, Any]:
    """Normalize a probe result into the dict ``store_printer_reading`` persists.

    Unreachable -> a minimal offline reading (so the printer reads "down", not
    gone). Live -> the full reading, with the candidate's MAC/name filling the
    gaps the generic SNMP profile does not carry.
    """
    if reading is None:
        return {
            "ip": cand.ip,
            "online": False,
            "status": "unreachable",
            "serial": None,
            "mac": cand.mac,
            "hostname": cand.name,
            "vendor": None,
            "model": None,
            "firmware": None,
            "uptime": None,
            "total_pages": None,
            "color_pages": None,
            "mono_pages": None,
            "duplex_pages": None,
            "supplies": [],
            "trays": [],
            "errors": [],
            "source_protocol": None,
            "sources": list(cand.sources),
        }
    d: dict[str, Any] = asdict(reading)  # frozen supplies/trays/errors -> list[dict]
    d["online"] = True
    d["ip"] = reading.ip or cand.ip
    d["mac"] = reading.mac or cand.mac
    d["hostname"] = reading.hostname or cand.name
    d["sources"] = list(cand.sources)
    return d


def run_poll_cycle(
    candidates: Sequence[PrinterCandidate],
    printer_cfg: PrinterConfig,
    *,
    probe: ProbeFn = collector.probe,
    store: StoreFn = db.store_printer_reading,
    now: Optional[str] = None,
    max_workers: int = _MAX_WORKERS,
) -> dict[str, int]:
    """Probe every candidate once and store the result. Returns a count summary."""
    polled = len(candidates)
    if polled == 0:
        return {"polled": 0, "online": 0, "unreachable": 0, "errors": 0}
    stamp = now or _now_iso()
    counts = {"online": 0, "unreachable": 0, "errors": 0}

    def work(cand: PrinterCandidate) -> str:
        try:
            reading = probe(
                cand.ip,
                community=printer_cfg.snmp_community,
                version=printer_cfg.snmp_version,
            )
            payload = _build_reading(cand, reading)
            pid = printer_identity(
                serial=payload.get("serial"),
                mac=payload.get("mac"),
                ip=payload.get("ip"),
            )
            store(pid, payload, received_at=stamp)
            return "online" if reading is not None else "unreachable"
        except Exception:  # noqa: BLE001 -- one bad host must not kill the whole cycle
            _log.exception("printer poll failed for %s", cand.ip)
            return "error"

    workers = min(max_workers, polled)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for tag in pool.map(work, candidates):
            if tag == "online":
                counts["online"] += 1
            elif tag == "unreachable":
                counts["unreachable"] += 1
            else:
                counts["errors"] += 1
    return {"polled": polled, **counts}


def poll_now(
    printer_cfg: PrinterConfig,
    *,
    get_hints: Callable[[], list[dict[str, Any]]] = db.get_printer_port_hints,
    get_snapshots: Callable[[], list[dict[str, Any]]] = db.get_network_snapshots,
    probe: ProbeFn = collector.probe,
    store: StoreFn = db.store_printer_reading,
    now: Optional[str] = None,
) -> dict[str, int]:
    """Build the candidate list from silent discovery, then run one poll cycle.

    Used by the lifespan loop and the dashboard force button. Never scans ranges.
    """
    candidates = discovery.merge(
        agent_hints=get_hints(),
        arp_snapshots=get_snapshots(),
        static_ips=printer_cfg.static_ips,
    )
    return run_poll_cycle(candidates, printer_cfg, probe=probe, store=store, now=now)
