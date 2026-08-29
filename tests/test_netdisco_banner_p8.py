"""Ф8 T6: active banner identification (TLS cert name + HTTP Server header).

The one active passive-tier collector -- a bounded TCP touch of 443/80 on an
already-known RFC1918 host (no more invasive than the reachability cycle, which
already connects to those ports). The two parsers are pure (peer-cert dict ->
name; header bag -> Server string) and fail-closed; the collector is RFC1918-
gated and capped, with injected fetchers so the suite never opens a socket.
RED first.
"""

from __future__ import annotations

from typing import List, Optional

from server.netdisco import banner

# --------------------------------------------------------------------------- #
# parse_cert_names                                                              #
# --------------------------------------------------------------------------- #


def test_cert_san_dns_wins() -> None:
    cert = {
        "subject": ((("commonName", "fallback.local"),),),
        "subjectAltName": (("DNS", "printer-7.office.local"), ("DNS", "alt.local")),
    }
    assert banner.parse_cert_names(cert) == "printer-7.office.local"


def test_cert_falls_back_to_common_name() -> None:
    cert = {"subject": ((("countryName", "US"),), (("commonName", "switch-core.lan"),))}
    assert banner.parse_cert_names(cert) == "switch-core.lan"


def test_cert_rejects_wildcard_and_junk() -> None:
    assert banner.parse_cert_names({"subjectAltName": (("DNS", "*.office.local"),)}) is None
    assert banner.parse_cert_names({"subject": ((("commonName", "bad name\n"),),)}) is None
    assert banner.parse_cert_names(None) is None
    assert banner.parse_cert_names({}) is None


# --------------------------------------------------------------------------- #
# parse_http_server                                                             #
# --------------------------------------------------------------------------- #


def test_http_server_extracted_and_bounded() -> None:
    assert (
        banner.parse_http_server({"Server": "HP HTTP Server; LaserJet"})
        == "HP HTTP Server; LaserJet"
    )
    # case-insensitive key
    assert banner.parse_http_server({"server": "nginx/1.0"}) == "nginx/1.0"
    long = "x" * 500
    assert len(banner.parse_http_server({"Server": long})) == 120


def test_http_server_missing_or_control_bytes_is_none() -> None:
    assert banner.parse_http_server({}) is None
    assert banner.parse_http_server(None) is None
    assert banner.parse_http_server({"Server": "bad\x00banner"}) is None


# --------------------------------------------------------------------------- #
# collect_banner                                                                #
# --------------------------------------------------------------------------- #


def test_collect_banner_maps_and_gates_rfc1918() -> None:
    def cert_fn(ip: str, _t: float) -> Optional[dict]:
        return {"subjectAltName": (("DNS", "dev.lan"),)} if ip == "10.0.0.5" else None

    def http_fn(ip: str, _t: float) -> Optional[dict]:
        return {"Server": "lighttpd"} if ip == "10.0.0.5" else None

    out = banner.collect_banner(
        ["10.0.0.5", "8.8.8.8"], cert_fn=cert_fn, http_fn=http_fn, timeout=0.1
    )
    assert "8.8.8.8" not in out  # public never touched
    assert out["10.0.0.5"].hostname == "dev.lan"
    assert out["10.0.0.5"].model == "lighttpd"
    assert out["10.0.0.5"].source == "banner"


def test_collect_banner_cap_and_fail_closed() -> None:
    seen: List[str] = []

    def cert_fn(ip: str, _t: float) -> Optional[dict]:
        seen.append(ip)
        return None  # nothing identifiable anywhere

    out = banner.collect_banner(
        ["10.0.0.1", "10.0.0.2", "10.0.0.3"],
        cert_fn=cert_fn,
        http_fn=lambda ip, t: None,
        cap=2,
        timeout=0.1,
    )
    assert out == {}  # both None -> no fabricated hint
    assert len(seen) == 2  # cap honoured


def test_collect_banner_cert_only_or_http_only() -> None:
    out = banner.collect_banner(
        ["10.0.0.7"],
        cert_fn=lambda ip, t: None,
        http_fn=lambda ip, t: {"Server": "Boa/0.94"},
        timeout=0.1,
    )
    assert out["10.0.0.7"].hostname is None
    assert out["10.0.0.7"].model == "Boa/0.94"
