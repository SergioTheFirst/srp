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
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence

from server import db
from server.printers import collector, discovery, scan
from server.printers import ipp as ipp_client
from server.printers.config import PrinterConfig
from server.printers.discovery import PrinterCandidate
from server.printers.models import PrinterReading, printer_identity

_log = logging.getLogger("srp.printers")

_MAX_WORKERS = 16  # bound the SNMP fan-out; printer fleets are small
_poll_lock = threading.Lock()  # serialize cycles: one fan-out at a time (anti-DoS)

ProbeFn = Callable[..., Optional[PrinterReading]]
StoreFn = Callable[..., None]
JobsProbeFn = Callable[..., list[dict[str, Any]]]
JobsStoreFn = Callable[..., None]


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


def _arp_only(sources: Sequence[str]) -> bool:
    """True when ARP is the ONLY way this candidate was found -- no spooler hint,
    config entry, or printer-port scan vouches that it is actually a printer."""
    s = set(sources)
    return bool(s) and s <= {"arp"}


def run_poll_cycle(
    candidates: Sequence[PrinterCandidate],
    printer_cfg: PrinterConfig,
    *,
    probe: ProbeFn = collector.probe,
    store: StoreFn = db.store_printer_reading,
    now: Optional[str] = None,
    max_workers: int = _MAX_WORKERS,
    is_confirmed: Callable[[str], bool] = db.printer_is_confirmed,
    jobs_probe: JobsProbeFn = ipp_client.get_completed_jobs,
    jobs_store: JobsStoreFn = db.store_printer_ipp_jobs,
) -> dict[str, int]:
    """Probe every candidate once and store the result. Returns a count summary.

    A bare ARP neighbour that does not answer as a printer is NOT minted as a
    phantom "unreachable" record -- it is some other LAN host, not a printer. An
    already-confirmed printer that is merely offline still records "unreachable"
    (down != gone); spooler/config/scan candidates are kept on their own merit.

    When ``printer_cfg.ipp_jobs`` is on, a printer that answered SNMP (live)
    also gets an IPP Get-Jobs probe for completed-job user attribution -- a
    supplementary source, never blocking/required (З.10-P1).
    """
    polled = len(candidates)
    if polled == 0:
        return {"polled": 0, "online": 0, "unreachable": 0, "errors": 0, "skipped": 0}
    stamp = now or _now_iso()
    counts = {"online": 0, "unreachable": 0, "errors": 0, "skipped": 0}

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
            if reading is None and _arp_only(cand.sources) and not is_confirmed(pid):
                return "skipped"  # not a printer, just an ARP neighbour -- don't store
            store(pid, payload, received_at=stamp)
            if reading is not None and printer_cfg.ipp_jobs:
                jobs = jobs_probe(cand.ip)
                if jobs:
                    jobs_store(pid, jobs, received_at=stamp)
            return "online" if reading is not None else "unreachable"
        except Exception:  # noqa: BLE001 -- one bad host must not kill the whole cycle
            _log.exception("printer poll failed for %s", cand.ip)
            return "errors"

    workers = min(max_workers, polled)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for tag in pool.map(work, candidates):
            counts[tag] += 1
    return {"polled": polled, **counts}


def poll_now(
    printer_cfg: PrinterConfig,
    *,
    get_hints: Callable[[], list[dict[str, Any]]] = db.get_printer_port_hints,
    get_snapshots: Callable[[], list[dict[str, Any]]] = db.get_network_snapshots,
    probe: ProbeFn = collector.probe,
    store: StoreFn = db.store_printer_reading,
    now: Optional[str] = None,
    scan_fn: Callable[[PrinterConfig], list[str]] = scan.scan,
    purge_phantoms: Callable[[], int] = db.delete_unconfirmed_arp_printers,
) -> dict[str, int]:
    """Build the candidate list from discovery (+ active scan when enabled), then
    run one poll cycle. Used by the lifespan loop and the dashboard force button.

    Active range scanning runs ONLY when PrinterConfig.active_scan is True
    (authorized 2026-06-19); otherwise this is silent discovery only.
    """
    # Serialize cycles: a second concurrent poll (button mashed, or the lifespan
    # loop firing while a manual poll runs) returns "busy" instead of launching
    # another full SNMP/IPP/HTTP fan-out (security review MEDIUM-1: anti-DoS).
    if not _poll_lock.acquire(blocking=False):
        return {"polled": 0, "online": 0, "unreachable": 0, "errors": 0, "busy": True}
    try:
        scan_ips = tuple(scan_fn(printer_cfg)) if printer_cfg.active_scan else ()
        candidates = discovery.merge(
            agent_hints=get_hints(),
            arp_snapshots=get_snapshots(),
            static_ips=printer_cfg.static_ips,
            scan_ips=scan_ips,
        )
        result = run_poll_cycle(candidates, printer_cfg, probe=probe, store=store, now=now)
        # Sweep any legacy phantom ARP rows created before the skip guard existed.
        purged = purge_phantoms()
        if purged:
            result = {**result, "purged": purged}
        return result
    finally:
        _poll_lock.release()
