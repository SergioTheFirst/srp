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
    cfg = PrinterConfig(active_scan=False, scan_cidrs=("192.168.77.0/30",))
    assert scan.scan(cfg, host_check=lambda ip: True) == []


def test_scan_uses_injected_check_over_enumerated_hosts():
    cfg = PrinterConfig(active_scan=True, scan_cidrs=("192.168.77.0/30",))
    found = scan.scan(cfg, host_check=lambda ip: ip == "192.168.77.1")
    assert found == ["192.168.77.1"]


def test_scan_public_cidr_yields_no_hosts():
    # Direct config (bypasses load filter); expand still drops every public host.
    cfg = PrinterConfig(active_scan=True, scan_cidrs=("8.8.8.0/30",))
    assert scan.scan(cfg, host_check=lambda ip: True) == []


# ------------------------------------------------------------------ #
# Saturation guard: a VPN / transparent proxy (tun2socks, Outline) answers EVERY
# TCP connect on the host running the server, so a whole /24 looks "alive".
# Seen live 2026-08-23: 496 phantom nodes from two saturated /24s. A range where
# (almost) every host answers is evidence of a proxy, not of 250 devices --
# UNKNOWN over false confidence: drop the range, keep the honest ones.
# ------------------------------------------------------------------ #


def test_drop_saturated_discards_a_fully_alive_range():
    hosts = [f"10.33.0.{i}" for i in range(1, 255)]
    kept, dropped = scan.drop_saturated(hosts, list(hosts))
    assert kept == []
    assert dropped == ["10.33.0"]  # F6: the dropped /24 range key is returned too


def test_drop_saturated_keeps_a_sparse_range_and_drops_only_the_saturated_one():
    sparse = [f"192.168.77.{i}" for i in range(1, 255)]
    full = [f"10.33.0.{i}" for i in range(1, 255)]
    found = ["192.168.77.7", "192.168.77.163"] + full
    kept, dropped = scan.drop_saturated(sparse + full, found)
    assert kept == ["192.168.77.7", "192.168.77.163"]
    assert dropped == ["10.33.0"]


def test_drop_saturated_ignores_tiny_ranges():
    # a /30 with both hosts up is a normal lab, not a proxy
    hosts = ["192.168.77.1", "192.168.77.2"]
    kept, dropped = scan.drop_saturated(hosts, list(hosts))
    assert kept == hosts
    assert dropped == []


def test_scan_applies_the_saturation_guard():
    cfg = PrinterConfig(active_scan=True, scan_cidrs=("10.33.0.0/24",))
    assert scan.scan(cfg, host_check=lambda ip: True) == []
