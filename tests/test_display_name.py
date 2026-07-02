"""display_name: единственный источник операторского имени устройства (З.2).

device_id никогда не возвращается как имя; фоллбек-каскад hostname -> model ->
chassis -> ip -> «Без названия»; шаблоны больше не рисуют dev-… самостоятельно.
"""

from __future__ import annotations

import pathlib

from server.db import NEUTRAL_NAME, display_name


def test_hostname_wins():
    assert display_name("PC-01", model="OptiPlex", chassis="desktop") == "PC-01"


def test_falls_back_to_model_then_chassis_then_ip():
    assert display_name(None, model="OptiPlex 7080") == "OptiPlex 7080"
    assert display_name("", model=None, chassis="laptop") == "laptop"
    assert display_name(None, ip="192.168.9.50") == "192.168.9.50"


def test_blank_and_whitespace_only_candidates_are_skipped():
    assert display_name("   ", model="OptiPlex") == "OptiPlex"


def test_never_returns_device_id_as_title():
    assert display_name(None, device_id="dev-1a2b3c4d") == NEUTRAL_NAME


def test_disambiguate_appends_short_suffix_only_when_empty():
    got = display_name(None, device_id="dev-1a2b3c4d", disambiguate=True)
    assert got.startswith(NEUTRAL_NAME) and "2b3c4d" in got
    assert display_name("PC-02", device_id="dev-x", disambiguate=True) == "PC-02"


def test_disambiguate_without_device_id_still_neutral():
    assert display_name(None, disambiguate=True) == NEUTRAL_NAME


def test_templates_have_no_device_id_fallback():
    """Пин З.2: ни один шаблон не рисует dev-… как имя устройства."""
    tpl_dir = pathlib.Path(__file__).resolve().parents[1] / "server" / "web" / "templates"
    offenders = [
        p.name for p in tpl_dir.glob("*.html") if "or d.device_id" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []
