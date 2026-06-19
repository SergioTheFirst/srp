"""Phase 7 — active LAN-segment scan: enumeration, RFC1918 rails, gating.

The scan is AUTHORIZED (owner, 2026-06-19) but must stay tightly bounded: RFC1918
only, host-capped, OFF unless active_scan. Network probing is injected so these
tests touch no real socket.
"""

from __future__ import annotations

import pytest
from server.printers import scan
from server.printers.config import PrinterConfig
from server.printers.discovery import is_rfc1918_cidr

pytestmark = pytest.mark.unit


def test_is_rfc1918_cidr_guard():
    assert is_rfc1918_cidr("192.168.0.0/16")
    assert is_rfc1918_cidr("10.0.0.0/8")
    assert is_rfc1918_cidr("172.16.0.0/12")
    assert not is_rfc1918_cidr("8.8.8.0/24")  # public
    assert not is_rfc1918_cidr("0.0.0.0/0")  # too broad, not within RFC1918
    assert not is_rfc1918_cidr("garbage")


def test_expand_cidrs_rfc1918_only_drops_public():
    hosts = scan.expand_cidrs(["192.168.99.0/30", "8.8.8.0/30"], max_hosts=100)
    assert hosts == ["192.168.99.1", "192.168.99.2"]  # /30 = 2 usable; public dropped


def test_expand_cidrs_respects_host_cap():
    assert len(scan.expand_cidrs(["10.0.0.0/24"], max_hosts=5)) == 5


def test_expand_cidrs_zero_max_is_killswitch():
    assert scan.expand_cidrs(["10.0.0.0/24"], max_hosts=0) == []


def test_expand_cidrs_dedups_overlap():
    hosts = scan.expand_cidrs(["192.168.1.0/30", "192.168.1.0/29"], max_hosts=100)
    assert len(hosts) == len(set(hosts))


def test_scan_disabled_returns_empty():
    cfg = PrinterConfig(active_scan=False, scan_cidrs=("192.168.9.0/30",))
    assert scan.scan(cfg, host_check=lambda ip: True) == []


def test_scan_uses_injected_check_over_enumerated_hosts():
    cfg = PrinterConfig(active_scan=True, scan_cidrs=("192.168.9.0/30",))
    found = scan.scan(cfg, host_check=lambda ip: ip == "192.168.9.1")
    assert found == ["192.168.9.1"]


def test_scan_public_cidr_yields_no_hosts():
    # Direct config (bypasses load filter); expand still drops every public host.
    cfg = PrinterConfig(active_scan=True, scan_cidrs=("8.8.8.0/30",))
    assert scan.scan(cfg, host_check=lambda ip: True) == []
