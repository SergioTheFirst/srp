"""Phase-2 network map: OUI seed, pure builder, subnet anomaly (no DB)."""

from __future__ import annotations

import pytest
from server.analytics.oui import normalize_mac, vendor_for_mac

pytestmark = pytest.mark.unit


def test_normalize_mac_forms():
    assert normalize_mac("00:50:56:aa:bb:cc") == "00-50-56-AA-BB-CC"
    assert normalize_mac("0050.56aa.bbcc") == "00-50-56-AA-BB-CC"
    assert normalize_mac("00-50-56-AA-BB-CC") == "00-50-56-AA-BB-CC"
    assert normalize_mac("garbage") is None
    assert normalize_mac("") is None
    assert normalize_mac(None) is None


def test_vendor_seed_hit_and_honest_unknown():
    assert vendor_for_mac("00:50:56:01:02:03") == "VMware"
    assert vendor_for_mac("B8-27-EB-99-88-77") == "Raspberry Pi"
    assert vendor_for_mac("F4-39-09-11-22-33") is None  # unknown OUI -> no invented vendor
    assert vendor_for_mac(None) is None
