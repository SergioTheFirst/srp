"""S13/X5 (2026-11-02): disk-card view helpers stay pinned to the agent and to the engine."""

from __future__ import annotations

from typing import Any, Optional

import pytest
from client.collectors import smart
from server.analytics.storage import _score_disk
from server.scoring.score100 import band_for_risk_score
from server.web.disk_card import SMART_ATTR_RU, disk_spare_color, nvme_cw_flags, smart_fallback


def test_attr_names_cover_exactly_the_agent_whitelist() -> None:
    assert set(SMART_ATTR_RU) == {str(i) for i in smart._ATTR_WHITELIST}


@pytest.mark.parametrize(
    ("attr", "raw", "expected"),
    [
        ("194", 42, 42.0),
        ("194", "42", 42.0),
        ("194", 450, None),  # 10xCelsius / packed layout: rejected, not misread
        ("194", 0, None),
        ("194", 91, None),
        ("9", 24670, 24670.0),
        ("9", 200_000, 200_000.0),
        ("9", 200_001, None),
        ("9", "x", None),
    ],
)
def test_smart_fallback_decodes_only_plausible_raw(
    attr: str, raw: Any, expected: Optional[float]
) -> None:
    assert smart_fallback({"smart_attrs": {attr: raw}}, attr) == expected


@pytest.mark.parametrize("disk", [None, [], {}, {"smart_attrs": None}, {"smart_attrs": {}}])
def test_smart_fallback_without_attrs_is_none(disk: Any) -> None:
    assert smart_fallback(disk, "194") is None


@pytest.mark.parametrize("bits", [1, 2, 4, 8, 16, 31])
def test_cw_flags_are_the_engines_own_hits(bits: int) -> None:
    """Label verbatim and the chip colour = the engine's band of the hit's own points."""
    factors = _score_disk({"nvme_critical_warning": bits}, None)[1]
    hits = [f for f in factors if f["label"].startswith("NVMe:")]
    chip = {"good": "good", "watch": "warn", "bad": "bad"}
    expected = [(f["label"], chip[band_for_risk_score(f["delta"])]) for f in hits]
    assert nvme_cw_flags(bits) == expected


def test_cw_temperature_bit_is_a_warning_not_a_failure() -> None:
    assert nvme_cw_flags(0b10) == [("NVMe: критическая температура", "warn")]


def test_cw_flags_parse_like_the_engine() -> None:
    assert nvme_cw_flags(None) == []
    assert nvme_cw_flags("8") == []  # the engine ignores a string CW too


def test_spare_colour_is_the_engines_spare_rule() -> None:
    def flagged(spare: float, threshold: float) -> bool:
        disk = {"nvme_spare_pct": spare, "nvme_spare_threshold_pct": threshold}
        return "spare_below_threshold" in _score_disk(disk, None)[2]["flags"]

    assert disk_spare_color(8, 10) == "bad" and flagged(8, 10)
    assert disk_spare_color(10, 10) == "good" and not flagged(10, 10)
    assert disk_spare_color(None, 10) == "na"
    assert disk_spare_color(10, None) == "na"
