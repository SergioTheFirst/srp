"""Контрастный hoverlabel на всех Plotly-графиках печати (З.6).

Общий сниппет ``_plotly_hover.html`` подключается на каждую страницу с
графиком и применяется к КАЖДОМУ layout-объекту -- никаких дублирующихся
inline-стилей на каждый график по отдельности.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def _seed_printer(db, pid: str = "prn-hover") -> None:
    db.store_printer_reading(
        pid,
        {
            "ip": "192.168.1.50",
            "online": True,
            "hostname": "PRN-1",
            "mac": "AA-BB-CC-DD-EE-01",
            "vendor": "hp",
            "model": "HP LaserJet 400",
            "serial": "hover",
            "status": "idle",
            "total_pages": 1000,
            "supplies": [],
            "trays": [],
            "errors": [],
            "source_protocol": "snmp",
            "sources": ["spooler"],
        },
    )


def test_print_page_defines_and_applies_hoverlabel(client) -> None:
    """2 literal source sites cover 5 chart draws: the hero chart sets it inline;
    baseLayout() sets it once and backs the other 4 (timeline, 2x renderBar for
    printers/users, departments) -- this counts source occurrences, not draws."""
    body = client.get("/print").text
    assert "function srpHoverLabel()" in body
    assert body.count("hoverlabel: srpHoverLabel()") == 2
    i_base = body.find("function baseLayout(")
    assert i_base != -1
    assert "hoverlabel: srpHoverLabel()" in body[i_base : i_base + 600]


def test_printers_page_defines_and_applies_hoverlabel(client) -> None:
    body = client.get("/printers").text
    assert "function srpHoverLabel()" in body
    assert "hoverlabel: srpHoverLabel()" in body


def test_printer_detail_page_defines_and_applies_hoverlabel(client) -> None:
    from server import db

    _seed_printer(db)
    body = client.get("/printers/prn-hover").text
    assert "function srpHoverLabel()" in body
    assert "hoverlabel: srpHoverLabel()" in body


def test_hover_snippet_reads_theme_tokens_not_hardcoded_colors() -> None:
    """Пин: hoverlabel адаптируется к теме через CSS-токены, не захардкожен."""
    from pathlib import Path

    snippet = (
        Path(__file__).resolve().parents[1] / "server" / "web" / "templates" / "_plotly_hover.html"
    ).read_text(encoding="utf-8")
    assert "getComputedStyle" in snippet
    assert "--panel-2" in snippet and "--text" in snippet
