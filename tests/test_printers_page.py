"""Phase 6 — /printers + /printers/{id} pages render (SSR + XSS pin)."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def _seed(db, pid="prn-sn-A", **kw):
    reading = {
        "ip": "192.168.1.5",
        "online": True,
        "status": "idle",
        "model": "HP LJ",
        "hostname": "PRN-A",
        "serial": "A",
        "total_pages": 100,
        "supplies": [],
        "trays": [],
        "errors": [],
        "sources": ["spooler"],
    }
    reading.update(kw)
    db.store_printer_reading(pid, reading)


def test_printers_page_empty_renders(client):
    r = client.get("/printers")
    assert r.status_code == 200
    assert "не обнаружены" in r.text


def test_printers_page_lists_printer(client):
    from server import db

    _seed(db)
    r = client.get("/printers")
    assert r.status_code == 200
    assert "HP LJ" in r.text and "192.168.1.5" in r.text


def test_printer_detail_page_renders(client):
    from server import db

    _seed(db)
    r = client.get("/printers/prn-sn-A")
    assert r.status_code == 200
    assert "192.168.1.5" in r.text and "PRN-A" in r.text


def test_printer_detail_page_404(client):
    assert client.get("/printers/nope").status_code == 404


def test_printers_page_escapes_hostile_strings(client):
    from server import db

    hostile = "</script><script>alert(1)</script>"
    _seed(db, pid="prn-sn-X", model=hostile, hostname=hostile, serial="X")
    r = client.get("/printers")
    assert r.status_code == 200
    # SSR autoescape + tojson on the JSON island must neutralize the break-out:
    # the raw executable tag must never appear verbatim.
    assert "<script>alert(1)</script>" not in r.text


def test_printers_nav_link_present(client):
    assert 'href="/printers"' in client.get("/").text


def test_attach_printers_places_into_subnet_cluster():
    from server.web.dashboard import _attach_printers_to_netmap

    m = {"clusters": [{"subnet_hint": "192.168.1.x", "others": [{"ip": "192.168.1.50"}]}]}
    printers = [
        {"ip": "192.168.1.50", "printer_id": "prn-sn-A", "model": "HP", "online": True},
        {"ip": "10.0.0.9", "printer_id": "prn-sn-B", "model": "Canon", "online": True},
    ]
    out = _attach_printers_to_netmap(m, printers)
    c = out["clusters"][0]
    assert len(c["printers"]) == 1 and c["printers"][0]["printer_id"] == "prn-sn-A"
    assert c["others"] == []  # matching ARP node folded in, no double node
    assert len(out["printers_unclustered"]) == 1
    assert out["printers_unclustered"][0]["printer_id"] == "prn-sn-B"


def test_printer_detail_escapes_hostile_strings(client):
    from server import db

    hostile = "</script><script>alert(2)</script>"
    _seed(
        db,
        pid="prn-sn-XD",
        serial=hostile,
        model=hostile,
        supplies=[
            {
                "name": hostile,
                "type": "toner",
                "class_": "consumed",
                "level": 5,
                "max": 100,
                "percent": 5,
                "unit": 4,
            }
        ],
    )
    r = client.get("/printers/prn-sn-XD")
    assert r.status_code == 200
    assert "<script>alert(2)</script>" not in r.text


def test_netmap_page_shows_unclustered_printer(client):
    from server import db

    _seed(db, pid="prn-sn-NET", ip="192.168.50.7", model="Net Printer")
    r = client.get("/netmap")
    assert r.status_code == 200
    assert "/printers/prn-sn-NET" in r.text


def test_printers_pages_chart_defers_init_to_domcontentloaded(client):
    # Plotly loads with `defer`, so the inline pages-history chart IIFE must wait
    # for DOMContentLoaded before init -- otherwise it runs at parse time with
    # Plotly still undefined and silently bails, leaving "Напечатано страниц по
    # принтерам" empty forever. Same fix already lives in print.html. pytest does
    # not execute the canvas JS, so pin the gate at the source level.
    h = client.get("/printers").text
    assert 'id="pages-series-data"' in h  # data island always emitted
    assert "DOMContentLoaded" in h
    # the regressed pattern was `if (!el || typeof Plotly === "undefined") return;`
    assert 'typeof Plotly === "undefined") return' not in h
