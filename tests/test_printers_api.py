"""Phase 6 — printer dashboard API: overview, detail, force-poll."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def _seed(db, pid="prn-sn-CNX", ip="192.168.1.50", **kw):
    reading = {
        "ip": ip,
        "online": True,
        "hostname": "PRN-1",
        "mac": "AA-BB-CC-DD-EE-01",
        "vendor": "hp",
        "model": "HP LaserJet 400",
        "serial": "CNX",
        "firmware": "1.0",
        "uptime": 1000,
        "status": "idle",
        "total_pages": 12000,
        "color_pages": None,
        "mono_pages": None,
        "duplex_pages": None,
        "supplies": [
            {
                "name": "Black",
                "type": "toner",
                "class_": "consumed",
                "level": 12,
                "max": 100,
                "percent": 12,
                "unit": 4,
            }
        ],
        "trays": [],
        "errors": [],
        "source_protocol": "snmp",
        "sources": ["spooler"],
    }
    reading.update(kw)
    db.store_printer_reading(pid, reading)


def test_list_printers_empty(client):
    body = client.get("/api/v1/printers").json()
    assert body["printers"] == [] and body["unmatched_software"] == []


def test_list_printers_returns_seeded(client):
    from server import db

    _seed(db)
    body = client.get("/api/v1/printers").json()
    assert len(body["printers"]) == 1
    p = body["printers"][0]
    assert p["ip"] == "192.168.1.50"
    assert p["total_pages"] == 12000
    assert p["low_supply_pct"] == 12
    assert p["online"] is True


def test_printer_detail_and_404(client):
    from server import db

    _seed(db)
    assert client.get("/api/v1/printers/prn-sn-CNX").json()["serial"] == "CNX"
    assert client.get("/api/v1/printers/nope").status_code == 404


def test_force_poll_empty_db_finds_no_candidates(client):
    # No spooler hints / ARP / static IPs in the test DB -> poll_now finds no
    # candidates and touches no network; returns a zeroed summary.
    r = client.post("/api/v1/printers/poll")
    assert r.status_code == 200
    assert r.json() == {"polled": 0, "online": 0, "unreachable": 0, "errors": 0, "skipped": 0}


def test_printers_poll_is_rate_limited_after_a_burst(client):
    # Unauthenticated force button for printer poll -> must be throttled (same as
    # discovery/poll and topology/poll).
    assert client.post("/api/v1/printers/poll").status_code == 200  # within budget
    statuses = {client.post("/api/v1/printers/poll").status_code for _ in range(40)}
    assert 429 in statuses  # the flood is throttled
