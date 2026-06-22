"""Phase 4 — poll scheduler: fan-out probe -> store; reachability; guards.

``run_poll_cycle`` probes a given candidate list concurrently and stores one
reading per candidate (live OR a synthetic "unreachable" reading, so a down
printer stays visible rather than vanishing). One bad host never kills the cycle
(device-ghost-cleanup lesson). ``poll_now`` wires discovery -> cycle for the
lifespan loop and the dashboard force button.

Pure: probe and store are injected, so these tests touch no real network/DB.
"""

from __future__ import annotations

import pytest
from server.printers import scheduler
from server.printers.config import PrinterConfig
from server.printers.discovery import PrinterCandidate
from server.printers.models import PrinterReading

pytestmark = pytest.mark.unit


def _cand(ip, mac=None, name=None, sources=("spooler",)) -> PrinterCandidate:
    return PrinterCandidate(ip=ip, mac=mac, name=name, sources=tuple(sources))


def test_poll_cycle_stores_live_and_unreachable():
    stored: list[tuple[str, dict]] = []

    def fake_probe(ip, **kw):
        if ip == "192.168.1.10":
            return PrinterReading(
                ip=ip, serial="CNX-1", vendor="hp", model="HP LJ", total_pages=12000, status="idle"
            )
        return None  # unreachable

    res = scheduler.run_poll_cycle(
        [_cand("192.168.1.10", mac="AA-BB-CC-DD-EE-01"), _cand("192.168.1.11")],
        PrinterConfig(),
        probe=fake_probe,
        store=lambda pid, r, received_at=None: stored.append((pid, r)),
        now="2026-06-19T10:00:00+00:00",
    )
    assert res == {"polled": 2, "online": 1, "unreachable": 1, "errors": 0, "skipped": 0}
    live = next(r for pid, r in stored if pid == "prn-sn-CNX-1")
    assert live["online"] is True and live["total_pages"] == 12000 and live["vendor"] == "hp"
    dead = next(r for pid, r in stored if pid == "prn-ip-192.168.1.11")
    assert dead["online"] is False and dead["status"] == "unreachable"


def test_poll_cycle_uses_candidate_mac_and_name_as_fallback():
    stored: list[tuple[str, dict]] = []

    def fake_probe(ip, **kw):
        return PrinterReading(ip=ip, status="idle")  # no serial / mac / hostname

    scheduler.run_poll_cycle(
        [_cand("192.168.1.10", mac="AA-BB-CC-DD-EE-09", name="Shared HP")],
        PrinterConfig(),
        probe=fake_probe,
        store=lambda pid, r, received_at=None: stored.append((pid, r)),
    )
    pid, reading = stored[0]
    assert pid == "prn-mac-AABBCCDDEE09"  # MAC fallback drives identity
    assert reading["mac"] == "AA-BB-CC-DD-EE-09"
    assert reading["hostname"] == "Shared HP"  # spooler share name fallback
    assert reading["sources"] == ["spooler"]


def test_poll_cycle_one_bad_host_does_not_kill_cycle():
    stored: list[tuple[str, dict]] = []

    def fake_probe(ip, **kw):
        if ip == "192.168.1.10":
            raise OSError("boom")
        return PrinterReading(ip=ip, serial="OK", status="idle")

    res = scheduler.run_poll_cycle(
        [_cand("192.168.1.10"), _cand("192.168.1.11")],
        PrinterConfig(),
        probe=fake_probe,
        store=lambda pid, r, received_at=None: stored.append((pid, r)),
    )
    assert res["polled"] == 2 and res["errors"] == 1
    assert any(pid == "prn-sn-OK" for pid, _ in stored)  # good host still stored


def test_poll_cycle_empty_candidates_is_noop():
    res = scheduler.run_poll_cycle(
        [], PrinterConfig(), probe=lambda *a, **k: None, store=lambda *a, **k: None
    )
    assert res == {"polled": 0, "online": 0, "unreachable": 0, "errors": 0, "skipped": 0}


def test_poll_cycle_skips_unconfirmed_arp_only_non_printer():
    # A bare ARP neighbour that does not answer as a printer must NOT be minted
    # as a phantom "unreachable" printer record.
    stored: list[tuple[str, dict]] = []
    res = scheduler.run_poll_cycle(
        [_cand("192.168.1.20", sources=("arp",))],
        PrinterConfig(),
        probe=lambda *a, **k: None,  # not a printer
        store=lambda pid, r, received_at=None: stored.append((pid, r)),
        is_confirmed=lambda pid: False,
    )
    assert res == {"polled": 1, "online": 0, "unreachable": 0, "errors": 0, "skipped": 1}
    assert stored == []  # nothing stored


def test_poll_cycle_arp_only_real_printer_is_stored():
    # ARP-discovered host that DOES answer as a printer is a real printer -> keep.
    stored: list[tuple[str, dict]] = []
    scheduler.run_poll_cycle(
        [_cand("192.168.1.21", sources=("arp",))],
        PrinterConfig(),
        probe=lambda ip, **kw: PrinterReading(ip=ip, serial="REAL", model="HP", status="idle"),
        store=lambda pid, r, received_at=None: stored.append((pid, r)),
        is_confirmed=lambda pid: False,
    )
    assert [pid for pid, _ in stored] == ["prn-sn-REAL"]


def test_poll_cycle_arp_only_already_confirmed_printer_kept_when_down():
    # A printer we already confirmed, now offline, must still record "unreachable"
    # (down != gone) even though it was only ever seen via ARP.
    stored: list[tuple[str, dict]] = []
    res = scheduler.run_poll_cycle(
        [_cand("192.168.1.22", mac="AA-BB-CC-DD-EE-22", sources=("arp",))],
        PrinterConfig(),
        probe=lambda *a, **k: None,
        store=lambda pid, r, received_at=None: stored.append((pid, r)),
        is_confirmed=lambda pid: True,
    )
    assert res["unreachable"] == 1 and res["skipped"] == 0
    assert stored and stored[0][1]["status"] == "unreachable"


def test_poll_cycle_spooler_unreachable_still_stored():
    # A spooler/config/scan source means a PC prints to it / engineer declared it
    # / it answered a printer port -> store even if currently unreachable.
    stored: list[tuple[str, dict]] = []
    res = scheduler.run_poll_cycle(
        [_cand("192.168.1.23", sources=("spooler",))],
        PrinterConfig(),
        probe=lambda *a, **k: None,
        store=lambda pid, r, received_at=None: stored.append((pid, r)),
        is_confirmed=lambda pid: False,
    )
    assert res["unreachable"] == 1 and res["skipped"] == 0 and len(stored) == 1


def test_poll_now_runs_scan_when_active():
    cfg = PrinterConfig(active_scan=True, scan_cidrs=("192.168.9.0/30",))
    stored: list[tuple[str, dict]] = []
    res = scheduler.poll_now(
        cfg,
        get_hints=lambda: [],
        get_snapshots=lambda: [],
        probe=lambda ip, **kw: PrinterReading(ip=ip, serial=ip, status="idle"),
        store=lambda pid, r, received_at=None: stored.append((pid, r)),
        scan_fn=lambda c: ["192.168.9.5"],
        purge_phantoms=lambda: 0,
    )
    assert res["polled"] == 1
    assert [r["ip"] for _, r in stored] == ["192.168.9.5"]


def test_poll_now_skips_scan_when_inactive():
    cfg = PrinterConfig(active_scan=False)
    called: list[str] = []
    scheduler.poll_now(
        cfg,
        get_hints=lambda: [],
        get_snapshots=lambda: [],
        probe=lambda *a, **k: None,
        store=lambda *a, **k: None,
        scan_fn=lambda c: called.append("scanned") or ["x"],
        purge_phantoms=lambda: 0,
    )
    assert called == []  # scan_fn must NOT run when active_scan is False


def test_poll_now_wires_discovery_to_cycle():
    cfg = PrinterConfig(static_ips=("192.168.1.50",))
    stored: list[tuple[str, dict]] = []
    res = scheduler.poll_now(
        cfg,
        get_hints=lambda: [{"name": "HP", "ip": "192.168.1.51"}],
        get_snapshots=lambda: [],
        probe=lambda ip, **kw: PrinterReading(ip=ip, serial=ip, status="idle"),
        store=lambda pid, r, received_at=None: stored.append((pid, r)),
        purge_phantoms=lambda: 0,
    )
    ips = sorted(r["ip"] for _, r in stored)
    assert ips == ["192.168.1.50", "192.168.1.51"]  # static list + spooler hint both polled
    assert res["polled"] == 2
