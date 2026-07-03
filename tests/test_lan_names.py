"""T2: agent-side NetBIOS naming of LAN neighbors.

The agent is the only host L2-adjacent to a remote site's LAN -- NBNS
(UDP/137) does not route off-subnet, so this is the only vantage point that
can name those neighbors. ``_parse_node_status`` is the pure byte->name
parser (ported from the server's tested ``server/netdisco/passive.py::
parse_netbios``, extended with suffix precedence); ``resolve_netbios_names``
is the bounded, RFC1918-only, fail-closed unicast collector around it. The
socket is injected so this suite never opens a real one. RED first.
"""

from __future__ import annotations

import socket
from typing import List, Tuple

from client.collectors import lan_names

# --------------------------------------------------------------------------- #
# byte-payload builders (mirrors tests/test_netdisco_passive_p8.py's helper)   #
# --------------------------------------------------------------------------- #


def _name_entry(name: str, suffix: int, *, group: bool = False) -> bytes:
    """One 18-byte NBSTAT name-table entry: 15-byte padded name + suffix + flags."""
    padded = (name.upper().ljust(15))[:15].encode("ascii")
    flags = 0x8000 if group else 0x0400  # group bit vs unique+active
    return padded + bytes([suffix]) + flags.to_bytes(2, "big")


def _nbstat_response(
    entries: List[bytes], *, rr_fixed: bytes = b"\x00\x21\x00\x01\x00\x00\x00\x00"
) -> bytes:
    # header: txn=0, flags=0x8400 (response/AA), qd=0, an=1, ns=0, ar=0
    hdr = b"\x00\x00\x84\x00\x00\x00\x00\x01\x00\x00\x00\x00"
    encoded = b"\x20" + b"A" * 32 + b"\x00"  # RR name field; content is never inspected
    rdata = bytes([len(entries)]) + b"".join(entries) + b"\x00" * 6  # num_names + entries + stats
    return hdr + encoded + rr_fixed + len(rdata).to_bytes(2, "big") + rdata


# --------------------------------------------------------------------------- #
# _parse_node_status -- pure parser                                            #
# --------------------------------------------------------------------------- #


def test_parse_extracts_workstation_name():
    data = _nbstat_response([_name_entry("DESKTOP-7", 0x00)])
    assert lan_names._parse_node_status(data) == "DESKTOP-7"


def test_parse_prefers_suffix_0x20_over_0x00():
    data = _nbstat_response([_name_entry("DESKTOP-7", 0x00), _name_entry("FILESRV", 0x20)])
    assert lan_names._parse_node_status(data) == "FILESRV"


def test_parse_skips_group_name_leaving_suffix_for_a_later_unique_entry():
    data = _nbstat_response(
        [_name_entry("WORKGROUP", 0x00, group=True), _name_entry("DESKTOP-7", 0x00)]
    )
    assert lan_names._parse_node_status(data) == "DESKTOP-7"


def test_parse_group_flag_checked_regardless_of_suffix():
    """A group-flagged 0x20 entry must not block/claim suffix priority -- the
    parser falls through to the next-priority unique 0x00 entry instead."""
    data = _nbstat_response(
        [_name_entry("SHARE-GRP", 0x20, group=True), _name_entry("DESKTOP-7", 0x00)]
    )
    assert lan_names._parse_node_status(data) == "DESKTOP-7"


def test_parse_strips_padding():
    data = _nbstat_response([_name_entry("PC1", 0x00)])
    assert lan_names._parse_node_status(data) == "PC1"


def test_parse_truncated_entry_stops_gracefully():
    # num_names claims 2 but the buffer only carries one full 18-byte entry.
    entry = _name_entry("DESKTOP-7", 0x00)
    hdr = b"\x00\x00\x84\x00\x00\x00\x00\x01\x00\x00\x00\x00"
    encoded = b"\x20" + b"A" * 32 + b"\x00"
    rr_fixed = b"\x00\x21\x00\x01\x00\x00\x00\x00"
    rdata = bytes([2]) + entry  # claims 2 names, only 1 fits -> must not crash
    data = hdr + encoded + rr_fixed + len(rdata).to_bytes(2, "big") + rdata
    assert lan_names._parse_node_status(data) == "DESKTOP-7"


def test_parse_fail_closed_on_short_packet():
    assert lan_names._parse_node_status(b"\x00\x00") is None


def test_parse_fail_closed_on_zero_answers():
    hdr = b"\x00\x00\x84\x00\x00\x00\x00\x00\x00\x00\x00\x00"  # an=0
    assert lan_names._parse_node_status(hdr) is None


def test_parse_fail_closed_on_garbage():
    assert lan_names._parse_node_status(b"not an nbns packet at all, just junk") is None


def test_parse_fail_closed_when_only_group_names_present():
    data = _nbstat_response([_name_entry("WORKGROUP", 0x00, group=True)])
    assert lan_names._parse_node_status(data) is None


def test_parse_ignores_rr_name_and_ttl_content():
    """Locale/content independence: only the answer count, entry suffix byte and
    group-flag bit drive the decision -- NBNS carries no textual status field,
    and the RR's own encoded-name/TTL bytes are structurally skipped over."""
    hdr = b"\x00\x00\x84\x00\x00\x00\x00\x01\x00\x00\x00\x00"
    weird_encoded_name = b"\x20" + b"\xff" * 32 + b"\x00"  # garbage, not "A"*32
    rr_fixed = b"\x00\x21\x00\x01\xde\xad\xbe\xef"  # nonsense TTL bytes
    entry = _name_entry("DESKTOP-7", 0x00)
    rdata = bytes([1]) + entry + b"\x00" * 6
    data = hdr + weird_encoded_name + rr_fixed + len(rdata).to_bytes(2, "big") + rdata
    assert lan_names._parse_node_status(data) == "DESKTOP-7"


def test_parse_fail_closed_on_header_promising_data_it_does_not_have():
    """an=1 but the packet is exactly 12 bytes (header only, no question/answer
    at all) -- the length-byte read must fail closed, not raise IndexError."""
    hdr = b"\x00\x00\x84\x00\x00\x00\x00\x01\x00\x00\x00\x00"
    assert len(hdr) == 12
    assert lan_names._parse_node_status(hdr) is None


def test_parse_fail_closed_on_zero_rdlength():
    hdr = b"\x00\x00\x84\x00\x00\x00\x00\x01\x00\x00\x00\x00"
    encoded = b"\x20" + b"A" * 32 + b"\x00"
    rr_fixed = b"\x00\x21\x00\x01\x00\x00\x00\x00"
    data = hdr + encoded + rr_fixed + (0).to_bytes(2, "big")  # RDLENGTH=0, no rdata
    assert lan_names._parse_node_status(data) is None


def test_parse_skips_entry_with_invalid_characters_in_name():
    """A name failing the alnum/-._  allow-list is dropped, not surfaced raw."""
    data = _nbstat_response([_name_entry("BAD!NAME", 0x00)])
    assert lan_names._parse_node_status(data) is None


def test_parse_skips_entry_with_blank_name():
    """A name field that is entirely spaces strips down to empty -> dropped."""
    data = _nbstat_response([_name_entry("", 0x00)])
    assert lan_names._parse_node_status(data) is None


# --------------------------------------------------------------------------- #
# resolve_netbios_names -- bounded unicast collector (injected socket)         #
# --------------------------------------------------------------------------- #


class _FakeSock:
    """Minimal datagram socket: replays a script of (data, addr) then times out."""

    def __init__(self, script: List[Tuple[bytes, Tuple[str, int]]]):
        self._script = list(script)
        self.sent: List[Tuple[bytes, Tuple[str, int]]] = []

    def settimeout(self, _t):  # noqa: D401
        pass

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def recvfrom(self, _bufsize):
        if self._script:
            return self._script.pop(0)
        raise socket.timeout()

    def close(self):
        pass


def test_resolve_returns_name_for_responder():
    resp = _nbstat_response([_name_entry("DESKTOP-7", 0x00)])
    sock = _FakeSock([(resp, ("10.0.0.3", 137))])
    out = lan_names.resolve_netbios_names(["10.0.0.3"], sock_factory=lambda: sock)
    assert out == {"10.0.0.3": "DESKTOP-7"}
    assert sock.sent and sock.sent[0][1] == ("10.0.0.3", 137)


def test_resolve_never_queries_public_ip():
    factory_calls = []

    def factory():
        factory_calls.append(1)
        return _FakeSock([])

    out = lan_names.resolve_netbios_names(["8.8.8.8"], sock_factory=factory)
    assert out == {}
    assert factory_calls == []  # socket never even opened for an all-public batch


def test_resolve_filters_public_from_mixed_batch():
    sock = _FakeSock([])
    lan_names.resolve_netbios_names(["8.8.8.8", "10.0.0.5", "1.1.1.1"], sock_factory=lambda: sock)
    sent_ips = [addr[0] for _, addr in sock.sent]
    assert sent_ips == ["10.0.0.5"]


def test_resolve_cap_bounds_fanout():
    ips = [f"10.0.0.{i}" for i in range(1, 251)]  # 250 distinct RFC1918 ips
    sock = _FakeSock([])
    lan_names.resolve_netbios_names(ips, cap=5, sock_factory=lambda: sock)
    assert len(sock.sent) == 5


def test_resolve_timeout_no_reply_yields_no_entry():
    sock = _FakeSock([])  # nobody answers
    out = lan_names.resolve_netbios_names(["10.0.0.5"], sock_factory=lambda: sock)
    assert out == {}


def test_resolve_dedupes_repeated_ip():
    sock = _FakeSock([])
    lan_names.resolve_netbios_names(["10.0.0.5", "10.0.0.5", "10.0.0.5"], sock_factory=lambda: sock)
    assert len(sock.sent) == 1


def test_resolve_multiple_responders():
    r1 = _nbstat_response([_name_entry("MEDPOST", 0x00)])
    r2 = _nbstat_response([_name_entry("SKPD3", 0x00)])
    sock = _FakeSock([(r1, ("192.168.9.6", 137)), (r2, ("192.168.9.25", 137))])
    out = lan_names.resolve_netbios_names(
        ["192.168.9.6", "192.168.9.25"], sock_factory=lambda: sock
    )
    assert out == {"192.168.9.6": "MEDPOST", "192.168.9.25": "SKPD3"}


def test_resolve_ignores_reply_from_unrequested_source():
    resp = _nbstat_response([_name_entry("STRANGER", 0x00)])
    sock = _FakeSock([(resp, ("10.0.0.99", 137))])  # not one of the requested targets
    out = lan_names.resolve_netbios_names(["10.0.0.5"], sock_factory=lambda: sock)
    assert out == {}


def test_resolve_socket_factory_failure_is_fail_closed():
    def boom():
        raise OSError("no sockets available")

    out = lan_names.resolve_netbios_names(["10.0.0.5"], sock_factory=boom)
    assert out == {}


def test_resolve_empty_input_returns_empty_without_opening_socket():
    calls = []

    def factory():
        calls.append(1)
        return _FakeSock([])

    out = lan_names.resolve_netbios_names([], sock_factory=factory)
    assert out == {}
    assert calls == []  # never even opens a socket for an empty batch


def test_resolve_malformed_reply_yields_no_entry_not_a_crash():
    sock = _FakeSock([(b"\x00\x00garbage", ("10.0.0.5", 137))])
    out = lan_names.resolve_netbios_names(["10.0.0.5"], sock_factory=lambda: sock)
    assert out == {}


def test_resolve_skips_malformed_ip_strings():
    sock = _FakeSock([])
    lan_names.resolve_netbios_names(["not-an-ip", "10.0.0.5"], sock_factory=lambda: sock)
    sent_ips = [addr[0] for _, addr in sock.sent]
    assert sent_ips == ["10.0.0.5"]


class _ResetSock(_FakeSock):
    """A socket whose recv fails with a plain OSError, distinct from a timeout."""

    def recvfrom(self, _bufsize):
        raise ConnectionResetError("connection reset")


def test_resolve_breaks_cleanly_on_plain_oserror_not_just_timeout():
    sock = _ResetSock([])
    out = lan_names.resolve_netbios_names(["10.0.0.5"], sock_factory=lambda: sock)
    assert out == {}
