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


def test_printer_detail_page_shows_ipp_jobs_when_present(client):
    from server import db

    _seed(db)
    db.store_printer_ipp_jobs(
        "prn-sn-A",
        [{"job_id": 1, "name": "report.pdf", "user_name": "ivanov", "impressions": 4}],
        received_at="2026-07-02T10:00:00+00:00",
    )
    r = client.get("/printers/prn-sn-A")
    assert r.status_code == 200
    assert "Последние задания (IPP)" in r.text
    assert "ivanov" in r.text and "report.pdf" in r.text


def test_printer_detail_page_hides_ipp_section_when_absent(client):
    from server import db

    _seed(db)
    r = client.get("/printers/prn-sn-A")
    assert "Последние задания (IPP)" not in r.text


def test_printer_detail_page_escapes_hostile_ipp_job_strings(client):
    from server import db

    hostile = "</script><script>alert(1)</script>"
    _seed(db)
    db.store_printer_ipp_jobs(
        "prn-sn-A",
        [{"job_id": 2, "name": hostile, "user_name": hostile, "impressions": 1}],
        received_at="2026-07-02T10:00:00+00:00",
    )
    r = client.get("/printers/prn-sn-A")
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;alert(1)" in r.text


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


def test_printers_page_table_wrap_sticky_header_anchors_to_wrapper_top(client):
    """Regression guard for the sticky-header bug: `<div style="overflow-x:auto">`
    (or a `.table-wrap` class with the same declaration) forces the div's
    overflow-y computed value to `auto` too (CSS Overflow spec), making the div
    itself the sticky positioning ancestor for `th` instead of the page -- the
    header then sticks var(--header-h) INSIDE the wrapper and its opaque
    background paints over the first data row underneath (confirmed on screen
    with Playwright, on `/printers`, `/device/<id>` and `/print` alike -- all
    three templates share the `.table-wrap` class). Fix: `.table-wrap th { top:0 }`
    re-anchors sticky to the wrapper's own top instead of the page header offset.
    It now lives ONCE in base.html (next to the global `th` rule it overrides)
    instead of being copy-pasted per template, so every current and future
    `.table-wrap` user is covered by construction.

    This is a template-source pin, not a rendering test: it proves the override
    rule is still present in the rendered `<style>` and that none of the three
    known `.table-wrap` pages has regressed to the bare unscoped inline wrapper
    (which would opt back out of the class -- and the fix). It cannot see an
    actual pixel overlap the way a screenshot can -- a future regression that
    changes --header-h itself, or breaks sticky some other way not involving this
    specific CSS text, would not be caught here; that class of regression needs a
    live screenshot re-check (see task-5b-report.md).
    """
    import re
    from datetime import datetime, timezone

    from server import db

    _seed(db, pid="prn-sticky")
    printers_html = client.get("/printers").text

    # The override rule is global (base.html), so any page's rendered <style>
    # proves it's still there -- checked once, not per page.
    assert re.search(r"\.table-wrap\s+th\s*\{[^}]*top:\s*0", printers_html), (
        "base.html's .table-wrap th { top:0 } override is missing from the rendered <style> block"
    )

    device_id = "dev-sticky-tablewrap"
    now = datetime.now(timezone.utc).isoformat()
    db.touch_device(device_id, now, "0.1.0", hostname="DEV-STICKY")
    db.store_scores(
        device_id,
        now,
        {
            "risk": {
                "day1_factors": {
                    "performance": [],
                    "reliability": [],
                    "wear": [],
                    "risk_exposure": [],
                },
                "classes": [],
                "domains": {},
            }
        },
    )
    db.store_events(
        device_id,
        [
            {
                "ts": now,
                "log": "System",
                "source": "wineventlog",
                "event_id": 41,
                "level": "Critical",
                "message": "Kernel-Power",
            }
        ],
    )
    device_html = client.get(f"/device/{device_id}").text

    print_html = client.get("/print").text

    for page_name, h in (
        ("/printers", printers_html),
        (f"/device/{device_id}", device_html),
        ("/print", print_html),
    ):
        assert 'class="table-wrap"' in h, f"{page_name}: .table-wrap wrapper missing"
        assert 'style="overflow-x:auto"' not in h, (
            f"{page_name}: bare inline overflow-x:auto wrapper is back "
            "(bypasses the .table-wrap class and its th override)"
        )


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
