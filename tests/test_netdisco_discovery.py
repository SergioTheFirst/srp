"""Phase 5: netdisco candidate gathering (generalizes printers.discovery.merge).

gather_candidates unions the non-agent discovery sources -- ARP neighbours,
the engineer's static list, and active-scan hits -- into one deduplicated
candidate list, RFC1918-rechecked, MAC outranking IP.
"""

from __future__ import annotations

from typing import Any

from server.netdisco.discovery import gather_candidates


def test_gather_unions_arp_static_and_scan_sources() -> None:
    snaps: list[dict[str, Any]] = [{"neighbors": [{"ip": "10.0.0.5", "mac": "AA-BB-CC-00-00-01"}]}]
    cands = gather_candidates(
        arp_snapshots=snaps, static_ips=("10.0.0.9",), scan_ips=("10.0.0.20",)
    )
    by_ip = {c.ip: c for c in cands}
    assert set(by_ip) == {"10.0.0.5", "10.0.0.9", "10.0.0.20"}
    assert "arp" in by_ip["10.0.0.5"].sources
    assert "config" in by_ip["10.0.0.9"].sources
    assert "scan" in by_ip["10.0.0.20"].sources


def test_gather_dedups_by_mac_across_ips() -> None:
    snaps: list[dict[str, Any]] = [
        {
            "neighbors": [
                {"ip": "10.0.0.6", "mac": "AA-BB-CC-00-00-01"},
                {"ip": "10.0.0.5", "mac": "AA-BB-CC-00-00-01"},
            ]
        }
    ]
    cands = gather_candidates(arp_snapshots=snaps)
    assert len(cands) == 1  # same MAC, two IPs -> one identity
    assert cands[0].mac == "AA-BB-CC-00-00-01"
    assert cands[0].ip == "10.0.0.5"  # lowest IP kept


def test_gather_drops_non_rfc1918_hosts() -> None:
    cands = gather_candidates(
        arp_snapshots=[{"neighbors": [{"ip": "8.8.8.8", "mac": "AA-BB-CC-00-00-09"}]}],
        static_ips=("1.1.1.1",),
        scan_ips=("9.9.9.9",),
    )
    assert cands == []  # public addresses never become candidates


def test_gather_empty_inputs_returns_empty() -> None:
    assert gather_candidates(arp_snapshots=[]) == []
