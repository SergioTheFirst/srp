"""Ф8 T3-T5: passive multicast/unicast identification parsers + collectors.

The parsers are pure byte->hint functions (the testable core); the collectors do
only the bounded socket I/O around them, with an injected socket so the suite
never opens a real one. Every parser is fail-closed: junk in -> ``None`` out, no
fabricated identity. Collectors only ever trust a response whose SOURCE address
is RFC1918/link-local, and stop at a hard cap. RED first.
"""

from __future__ import annotations

import socket
from typing import List, Tuple

from server.netdisco import passive

# --------------------------------------------------------------------------- #
# byte-payload builders                                                        #
# --------------------------------------------------------------------------- #


def _dns_name(name: str) -> bytes:
    out = b""
    for label in name.split("."):
        out += bytes([len(label)]) + label.encode("ascii")
    return out + b"\x00"


def _mdns_response() -> bytes:
    # header: id=0, flags=0x8400 (response/AA), qd=1, an=2, ns=0, ar=0
    hdr = b"\x00\x00\x84\x00\x00\x01\x00\x02\x00\x00\x00\x00"
    enum = "_services._dns-sd._udp.local"
    q = _dns_name(enum) + b"\x00\x0c\x00\x01"  # PTR / IN
    # answer 1: PTR enum -> _ipp._tcp.local
    rdata1 = _dns_name("_ipp._tcp.local")
    a1 = _dns_name(enum) + b"\x00\x0c\x00\x01\x00\x00\x00\x78"
    a1 += len(rdata1).to_bytes(2, "big") + rdata1
    # answer 2: A record  Office-Printer.local -> 10.0.0.9
    a2 = _dns_name("Office-Printer.local") + b"\x00\x01\x00\x01\x00\x00\x00\x78"
    a2 += (4).to_bytes(2, "big") + bytes([10, 0, 0, 9])
    return hdr + q + a1 + a2


def _ssdp_response(server: str, st: str) -> bytes:
    return (
        "HTTP/1.1 200 OK\r\n"
        "CACHE-CONTROL: max-age=1800\r\n"
        f"SERVER: {server}\r\n"
        f"ST: {st}\r\n"
        "LOCATION: http://10.0.0.7:80/desc.xml\r\n"
        "\r\n"
    ).encode("ascii")


def _netbios_nbstat(name: str) -> bytes:
    # header: txn, flags=0x8400, qd=0, an=1, ns=0, ar=0
    hdr = b"\x00\x00\x84\x00\x00\x00\x00\x01\x00\x00\x00\x00"
    encoded = b"\x20" + b"A" * 32 + b"\x00"  # 32-byte encoded '*' name + null
    rr_fixed = b"\x00\x21\x00\x01\x00\x00\x00\x00"  # type NBSTAT, class IN, TTL 0
    padded = (name.upper().ljust(15))[:15].encode("ascii")
    entry = padded + b"\x00" + b"\x04\x00"  # suffix 0x00 (workstation), unique+active
    rdata = bytes([1]) + entry + b"\x00" * 6  # num_names=1 + entry + 6 stat bytes
    rr = encoded + rr_fixed + len(rdata).to_bytes(2, "big") + rdata
    return hdr + rr


def _wsd_probematch(types: str) -> bytes:
    return (
        '<?xml version="1.0"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
        "<s:Body><d:ProbeMatches><d:ProbeMatch>"
        f"<d:Types>{types}</d:Types>"
        "</d:ProbeMatch></d:ProbeMatches></s:Body></s:Envelope>"
    ).encode("utf-8")


class FakeSock:
    """Minimal datagram socket: replays a script of (data, addr) then times out."""

    def __init__(self, script: List[Tuple[bytes, Tuple[str, int]]]):
        self._script = list(script)
        self.sent: List[Tuple[bytes, Tuple[str, int]]] = []

    def settimeout(self, _t):  # noqa: D401
        pass

    def setsockopt(self, *_a):
        pass

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def recvfrom(self, _bufsize):
        if self._script:
            return self._script.pop(0)
        raise socket.timeout()

    def close(self):
        pass


# --------------------------------------------------------------------------- #
# parsers                                                                       #
# --------------------------------------------------------------------------- #


def test_parse_mdns_extracts_printer_subtype_and_hostname():
    hint = passive.parse_mdns(_mdns_response(), "10.0.0.9")
    assert hint is not None
    assert hint.subtype == "printer"
    assert hint.hostname == "Office-Printer"
    assert hint.source == "mdns"


def test_parse_mdns_fail_closed_on_junk():
    assert passive.parse_mdns(b"\xff\xff not dns", "10.0.0.9") is None


def test_parse_ssdp_router_from_igd_and_server():
    hint = passive.parse_ssdp(
        _ssdp_response(
            "Linux/3.10 UPnP/1.0", "urn:schemas-upnp-org:device:InternetGatewayDevice:1"
        ),
        "10.0.0.7",
    )
    assert hint is not None
    assert hint.subtype == "router"
    assert hint.source == "ssdp"


def test_parse_ssdp_printer_from_st():
    hint = passive.parse_ssdp(
        _ssdp_response("", "urn:schemas-upnp-org:device:Printer:1"), "10.0.0.7"
    )
    assert hint is not None and hint.subtype == "printer"


def test_parse_ssdp_fail_closed_when_no_device_type():
    assert passive.parse_ssdp(b"garbage\r\n\r\n", "10.0.0.7") is None


def test_parse_netbios_extracts_workstation_name():
    hint = passive.parse_netbios(_netbios_nbstat("DESKTOP-7"), "10.0.0.3")
    assert hint is not None
    assert hint.hostname == "DESKTOP-7"
    assert hint.subtype == "workstation"
    assert hint.source == "netbios"


def test_parse_netbios_fail_closed_on_short_packet():
    assert passive.parse_netbios(b"\x00\x00", "10.0.0.3") is None


def test_parse_wsd_printer_and_computer():
    assert passive.parse_wsd(_wsd_probematch("pub:Computer"), "10.0.0.4").subtype == "workstation"
    p = passive.parse_wsd(_wsd_probematch("wprt:PrintDeviceType"), "10.0.0.5")
    assert p is not None and p.subtype == "printer"


def test_parse_wsd_fail_closed_when_no_known_type():
    assert passive.parse_wsd(_wsd_probematch("d:UnknownThing"), "10.0.0.4") is None


# --------------------------------------------------------------------------- #
# collector gate                                                                #
# --------------------------------------------------------------------------- #


def test_collect_ssdp_drops_public_source_and_caps():
    local = (_ssdp_response("", "urn:schemas-upnp-org:device:Printer:1"), ("10.0.0.7", 1900))
    public = (_ssdp_response("", "urn:schemas-upnp-org:device:Printer:1"), ("8.8.8.8", 1900))
    second_local = (
        _ssdp_response("", "urn:schemas-upnp-org:device:InternetGatewayDevice:1"),
        ("10.0.0.8", 1900),
    )
    sock = FakeSock([local, public, second_local])
    out = passive.collect_ssdp(sock_factory=lambda: sock, cap=1, timeout=0.1)
    assert "8.8.8.8" not in out  # public source never trusted
    assert len(out) == 1  # cap honoured
    assert "10.0.0.7" in out
    assert sock.sent and sock.sent[0][1][1] == 1900  # an M-SEARCH actually went out
