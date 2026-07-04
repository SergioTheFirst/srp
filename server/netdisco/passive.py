"""Passive / multicast identification for netdisco (Ф8 T3-T5).

De-anonymises ``unknown`` nodes with the standard local-segment discovery
protocols every OS already speaks: mDNS/DNS-SD (5353), SSDP/UPnP (1900), NetBIOS
node-status (137) and WS-Discovery (3702). Each protocol has a **pure parser**
(bytes -> :class:`PassiveHint`, the testable core) and a **thin collector** that
does only the bounded socket I/O around it.

Safety invariants (every collector, no exceptions):
  * multicast egress is TTL-1 -- a probe physically cannot leave the local segment;
  * a response is trusted only when its SOURCE address is RFC1918/link-local;
  * collection stops at a hard ``cap`` and an overall timeout deadline;
  * parsers are fail-closed -- malformed bytes yield ``None``, never a guess.

The identity produced here is a LOW-priority hint: a real agent/SNMP name always
wins (the assembler prefers those and the writer only fills an empty field). The
socket factory is injectable so the suite never opens a real socket.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import ipaddress
import socket
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from server.printers.discovery import is_rfc1918

_MDNS_GROUP: Tuple[str, int] = ("224.0.0.251", 5353)
_SSDP_GROUP: Tuple[str, int] = ("239.255.255.250", 1900)
_WSD_GROUP: Tuple[str, int] = ("239.255.255.250", 3702)
_NETBIOS_PORT = 137

_DEFAULT_CAP = 512  # hard ceiling on hosts identified per protocol per cycle
_DEFAULT_TIMEOUT = 2.0  # socket read timeout AND the basis for the overall deadline
_BUFSIZE = 4096


@dataclass(frozen=True)
class PassiveHint:
    """A low-priority identity hint discovered passively for one host."""

    ip: str
    source: str
    hostname: Optional[str] = None
    subtype: Optional[str] = None
    model: Optional[str] = None


_Parser = Callable[[bytes, str], Optional[PassiveHint]]


def _is_local(ip: str) -> bool:
    """True only for RFC1918 or link-local addresses (the trust boundary)."""
    if not ip:
        return False
    if is_rfc1918(ip):
        return True
    try:
        return ipaddress.ip_address(ip).is_link_local
    except ValueError:
        return False


_MAX_NAME = 253  # DNS name length ceiling


def _clean_host(name: Optional[str]) -> Optional[str]:
    """A discovered name trimmed to a safe hostname, or ``None`` (fail-closed).

    The same allow-list the reverse-DNS / banner paths apply at their trust
    boundary: a control byte, U+FFFD, wildcard, whitespace or injection character
    drops the name rather than letting it flow into an identity field and a
    dashboard tooltip."""
    if not isinstance(name, str):
        return None
    host = name.strip().rstrip(".")
    if not host or len(host) > _MAX_NAME or "*" in host:
        return None
    if not all(c.isalnum() or c in "-._" for c in host):
        return None
    return host


# --------------------------------------------------------------------------- #
# DNS / mDNS                                                                    #
# --------------------------------------------------------------------------- #


def _encode_dns_name(name: str) -> bytes:
    out = b""
    for label in name.split("."):
        out += bytes([len(label)]) + label.encode("ascii")
    return out + b"\x00"


def _read_name(data: bytes, off: int) -> Tuple[str, int]:
    """Decode a (possibly compressed) DNS name; return ``(name, next_offset)``."""
    labels: List[str] = []
    next_off = off
    cur = off
    jumped = False
    hops = 0
    while hops < 32 and 0 <= cur < len(data):
        length = data[cur]
        if length == 0:
            cur += 1
            if not jumped:
                next_off = cur
            break
        if length & 0xC0 == 0xC0:
            if cur + 1 >= len(data):
                break
            ptr = ((length & 0x3F) << 8) | data[cur + 1]
            if not jumped:
                next_off = cur + 2
            jumped = True
            cur = ptr
            hops += 1
            continue
        cur += 1
        labels.append(data[cur : cur + length].decode("ascii", "replace"))
        cur += length
    return ".".join(labels), next_off


_MDNS_SERVICE_SUBTYPE = {
    "_ipp": "printer",
    "_ipps": "printer",
    "_printer": "printer",
    "_pdl-datastream": "printer",
    "_scanner": "printer",
    "_uscan": "printer",
    "_airplay": "media",
    "_raop": "media",
    "_googlecast": "media",
    "_spotify-connect": "media",
    "_smb": "workstation",
    "_workstation": "workstation",
    "_afpovertcp": "workstation",
    "_device-info": "workstation",
}
_MDNS_RESERVED_LABELS = {"_services", "_tcp", "_udp", "_dns-sd", "local"}


def parse_mdns(data: bytes, src_ip: str) -> Optional[PassiveHint]:
    """Service type + ``.local`` hostname from an mDNS message, else ``None``."""
    if not data or len(data) < 12:
        return None
    names: List[str] = []
    try:
        qd = int.from_bytes(data[4:6], "big")
        rr_count = sum(int.from_bytes(data[i : i + 2], "big") for i in (6, 8, 10))
        off = 12
        for _ in range(qd):
            name, off = _read_name(data, off)
            names.append(name)
            off += 4  # qtype + qclass
        for _ in range(rr_count):
            name, off = _read_name(data, off)
            names.append(name)
            rtype = int.from_bytes(data[off : off + 2], "big")
            off += 8  # type + class + ttl
            rdlen = int.from_bytes(data[off : off + 2], "big")
            off += 2
            if rtype in (12, 33):  # PTR / SRV carry a domain name in their rdata
                tgt, _ = _read_name(data, off + (6 if rtype == 33 else 0))
                if tgt:
                    names.append(tgt)
            off += rdlen
    except (IndexError, ValueError):
        return None
    subtype: Optional[str] = None
    hostname: Optional[str] = None
    for nm in names:
        labels = nm.lower().split(".")
        for token, stype in _MDNS_SERVICE_SUBTYPE.items():
            if token in labels:
                subtype = subtype or stype
        if nm.lower().endswith(".local") and nm.count(".") == 1 and not nm.startswith("_"):
            cand = _clean_host(nm.split(".")[0])
            if cand and cand.lower() not in _MDNS_RESERVED_LABELS:
                hostname = hostname or cand
    if not subtype and not hostname:
        return None
    return PassiveHint(ip=src_ip, source="mdns", hostname=hostname, subtype=subtype)


# --------------------------------------------------------------------------- #
# SSDP / UPnP                                                                   #
# --------------------------------------------------------------------------- #

_SSDP_DEVICE_SUBTYPE = (
    ("internetgatewaydevice", "router"),
    ("wandevice", "router"),
    ("printbasic", "printer"),
    ("printer", "printer"),
    ("mediaserver", "media"),
    ("mediarenderer", "media"),
)


def parse_ssdp(data: bytes, src_ip: str) -> Optional[PassiveHint]:
    """UPnP device class (from ST/NT) + SERVER banner, else ``None``."""
    text = data.decode("ascii", "replace")
    headers: Dict[str, str] = {}
    for line in text.split("\r\n"):
        if ":" in line:
            key, _, val = line.partition(":")
            headers[key.strip().lower()] = val.strip()
    st = (headers.get("st") or headers.get("nt") or "").lower()
    subtype: Optional[str] = None
    for token, stype in _SSDP_DEVICE_SUBTYPE:
        if token in st:
            subtype = stype
            break
    if not subtype:
        return None
    model = headers.get("server") or None
    return PassiveHint(ip=src_ip, source="ssdp", subtype=subtype, model=model)


# --------------------------------------------------------------------------- #
# NetBIOS node status                                                           #
# --------------------------------------------------------------------------- #


def parse_netbios(data: bytes, src_ip: str) -> Optional[PassiveHint]:
    """Windows machine name from an NBSTAT node-status reply, else ``None``."""
    try:
        if len(data) < 12 or int.from_bytes(data[6:8], "big") < 1:
            return None
        off = 12
        name_len = data[off]
        off += 1 + name_len + 1  # length byte + encoded name + null terminator
        off += 2 + 2 + 4  # type + class + ttl
        rdlen = int.from_bytes(data[off : off + 2], "big")
        off += 2
        if rdlen < 1 or off >= len(data):
            return None
        num = data[off]
        off += 1
        best: Optional[str] = None
        for _ in range(num):
            entry = data[off : off + 18]
            off += 18
            if len(entry) < 18:
                break
            suffix = entry[15]
            is_group = bool(int.from_bytes(entry[16:18], "big") & 0x8000)
            nm = _clean_host(entry[:15].split(b"\x00")[0].decode("ascii", "replace").strip())
            if suffix == 0x00 and not is_group and nm and best is None:
                best = nm
        if not best:
            return None
        return PassiveHint(ip=src_ip, source="netbios", hostname=best, subtype="workstation")
    except (IndexError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# WS-Discovery                                                                  #
# --------------------------------------------------------------------------- #

_WSD_TYPE_SUBTYPE = (
    ("printdevice", "printer"),
    ("printbasic", "printer"),
    ("printer", "printer"),
    ("computer", "workstation"),
    ("mediadevice", "media"),
)


def parse_wsd(data: bytes, src_ip: str) -> Optional[PassiveHint]:
    """Device class from a WS-Discovery ProbeMatch (string scan, no XML parser)."""
    text = data.decode("utf-8", "replace").lower()
    if "probematch" not in text:
        return None
    for token, stype in _WSD_TYPE_SUBTYPE:
        if token in text:
            return PassiveHint(ip=src_ip, source="wsd", subtype=stype)
    return None


# --------------------------------------------------------------------------- #
# P1: agent-relayed captures                                                   #
# --------------------------------------------------------------------------- #
#
# The agent (client/collectors/lan_discovery.py) is L2-adjacent to LANs this
# server may not be; it relays a raw capture instead of re-implementing the
# parsers above. One wire-format implementation, two capture vantage points.

_RELAYED_PARSERS: Dict[str, _Parser] = {"mdns": parse_mdns, "ssdp": parse_ssdp, "wsd": parse_wsd}


def parse_relayed_hint(record: Dict[str, Any]) -> Optional[PassiveHint]:
    """Decode one agent-relayed raw capture and parse it with the SAME parser
    used for this server's own local multicast capture, else ``None``
    (fail-closed on anything malformed/untrusted)."""
    if not isinstance(record, dict):
        return None
    source = record.get("source")
    parser = _RELAYED_PARSERS.get(source) if isinstance(source, str) else None
    ip = record.get("ip")
    b64 = record.get("data_b64")
    if parser is None or not isinstance(ip, str) or not _is_local(ip) or not isinstance(b64, str):
        return None
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return None
    return parser(data, ip)


# --------------------------------------------------------------------------- #
# collectors                                                                    #
# --------------------------------------------------------------------------- #


def _udp_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # TTL 1: a multicast probe physically cannot leave the local segment.
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    return sock


def _drain(
    sock: Any,
    parser: _Parser,
    *,
    cap: int,
    timeout: float,
) -> Dict[str, PassiveHint]:
    out: Dict[str, PassiveHint] = {}
    deadline = time.monotonic() + max(0.1, timeout) * 4
    while len(out) < cap and time.monotonic() < deadline:
        try:
            data, addr = sock.recvfrom(_BUFSIZE)
        except socket.timeout:
            break
        except OSError:
            break
        src = addr[0] if addr else ""
        if not _is_local(src) or src in out:
            continue
        hint = parser(data, src)
        if hint:
            out[src] = hint
    return out


def _query_collect(
    sock_factory: Callable[[], Any],
    query: bytes,
    dest: Tuple[str, int],
    parser: _Parser,
    *,
    cap: int,
    timeout: float,
) -> Dict[str, PassiveHint]:
    try:
        sock = sock_factory()
    except OSError:
        return {}
    try:
        sock.settimeout(timeout)
        try:
            sock.sendto(query, dest)
        except OSError:
            return {}
        return _drain(sock, parser, cap=cap, timeout=timeout)
    finally:
        with contextlib.suppress(OSError):
            sock.close()


def _mdns_query() -> bytes:
    hdr = b"\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00"  # qd=1
    # PTR (0x000c), qclass IN with the top "unicast-response (QU)" bit set (0x8001)
    # so responders unicast back to our ephemeral port instead of multicasting.
    return hdr + _encode_dns_name("_services._dns-sd._udp.local") + b"\x00\x0c\x80\x01"


def _ssdp_msearch() -> bytes:
    return (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 1\r\n"
        "ST: ssdp:all\r\n\r\n"
    ).encode("ascii")


def _wsd_probe() -> bytes:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" '
        'xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
        'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">'
        "<s:Header>"
        "<a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>"
        "<a:MessageID>urn:uuid:srp-netdisco-probe</a:MessageID>"
        "<a:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To>"
        "</s:Header><s:Body><d:Probe/></s:Body></s:Envelope>"
    ).encode("utf-8")


def _nbstat_query() -> bytes:
    # txn id, flags 0, qd=1; NBSTAT (0x21/IN) for the wildcard '*' name.
    hdr = b"\x00\x00\x00\x00\x00\x01\x00\x00\x00\x00\x00\x00"
    name = b"\x20" + b"CKAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" + b"\x00"
    return hdr + name + b"\x00\x21\x00\x01"


def collect_mdns(
    *,
    sock_factory: Callable[[], Any] = _udp_socket,
    cap: int = _DEFAULT_CAP,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Dict[str, PassiveHint]:
    return _query_collect(
        sock_factory, _mdns_query(), _MDNS_GROUP, parse_mdns, cap=cap, timeout=timeout
    )


def collect_ssdp(
    *,
    sock_factory: Callable[[], Any] = _udp_socket,
    cap: int = _DEFAULT_CAP,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Dict[str, PassiveHint]:
    return _query_collect(
        sock_factory, _ssdp_msearch(), _SSDP_GROUP, parse_ssdp, cap=cap, timeout=timeout
    )


def collect_wsd(
    *,
    sock_factory: Callable[[], Any] = _udp_socket,
    cap: int = _DEFAULT_CAP,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Dict[str, PassiveHint]:
    return _query_collect(
        sock_factory, _wsd_probe(), _WSD_GROUP, parse_wsd, cap=cap, timeout=timeout
    )


def collect_netbios(
    targets: Iterable[str],
    *,
    sock_factory: Callable[[], Any] = _udp_socket,
    cap: int = _DEFAULT_CAP,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Dict[str, PassiveHint]:
    """Unicast NBSTAT to each RFC1918 target, then collect node-status replies."""
    try:
        sock = sock_factory()
    except OSError:
        return {}
    try:
        sock.settimeout(timeout)
        query = _nbstat_query()
        sent = 0
        for ip in targets:
            if sent >= cap:
                break
            if not is_rfc1918(ip):
                continue
            try:
                sock.sendto(query, (ip, _NETBIOS_PORT))
            except OSError:
                continue
            sent += 1
        return _drain(sock, parse_netbios, cap=cap, timeout=timeout)
    finally:
        with contextlib.suppress(OSError):
            sock.close()
