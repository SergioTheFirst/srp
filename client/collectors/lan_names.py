"""Agent-side NetBIOS naming of LAN neighbors (client, additive, T2).

The agent is the only host L2-adjacent to a remote site's LAN: NBNS (UDP/137)
does not route off-subnet, so the server's passive NetBIOS collector
(``server/netdisco/passive.py::collect_netbios``) names nothing on a remote
site. This module ports that collector's query/parse (the tested reference)
into a pure-stdlib client module -- ``client/`` may never import ``server.*``.

Safety invariants (mirrors the server passive collectors):
  * RFC1918-only -- a public IP is never queried (the agent's privacy contract);
  * bounded fan-out (``cap``), a short per-socket timeout, and an overall
    wall-clock deadline, so a hung/silent segment can never stall the collector;
  * fail-closed -- a non-responding or malformed reply yields NO name, never a
    fabricated one;
  * locale-independent -- the parser reads only numeric suffix codes and the
    group/unique flag bit; NBNS carries no textual status field to depend on.

``sock_factory`` is injectable so the test-suite never opens a real socket.
"""

from __future__ import annotations

import contextlib
import ipaddress
import socket
import struct
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

_NETBIOS_PORT = 137
_BUFSIZE = 4096
_MAX_NETBIOS_NAME = 15  # NBNS name-field width

_DEFAULT_CAP = 128  # hard ceiling on hosts queried per collection
_DEFAULT_TIMEOUT = 0.5  # per-socket recv timeout (seconds)
_DEFAULT_DEADLINE = 2.5  # overall wall-clock budget for the whole batch (seconds)

# Suffix precedence: server/file-share (0x20) beats plain workstation (0x00).
_SUFFIX_PRIORITY = (0x20, 0x00)
_GROUP_FLAG = 0x8000

# Same three RFC1918 blocks enforced in client/collectors/network.py -- kept
# local (not imported): network.py imports resolve_netbios_names from here, so
# importing network.py back would create a cycle.
_RFC1918 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def _is_rfc1918(ip: Optional[str]) -> bool:
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _RFC1918)


def _encode_nbname(name16: bytes) -> bytes:
    """NetBIOS first-level encoding (RFC 1001 S14.1): each byte -> two nibbles,
    each nibble offset into 'A'..'P'."""
    out = bytearray()
    for byte in name16:
        out.append(0x41 + (byte >> 4))
        out.append(0x41 + (byte & 0x0F))
    return bytes(out)


def _nbstat_query() -> bytes:
    """A Node Status Request for the wildcard ``*`` name (RFC 1002 S4.2.1/18)."""
    header = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0)  # txn id/flags/qd=1/an/ns/ar
    wildcard = b"*" + b"\x00" * 15
    name = bytes([0x20]) + _encode_nbname(wildcard) + b"\x00"
    footer = struct.pack(">HH", 0x0021, 0x0001)  # qtype NBSTAT, qclass IN
    return header + name + footer


_QUERY = _nbstat_query()


def _clean_name(raw: bytes) -> Optional[str]:
    text = raw.split(b"\x00")[0].decode("ascii", "replace").strip()
    if not text or len(text) > _MAX_NETBIOS_NAME or "*" in text:
        return None
    if not all(c.isalnum() or c in "-._" for c in text):
        return None
    return text


def _parse_node_status(data: bytes) -> Optional[str]:
    """Best unique NetBIOS name from an NBSTAT response, else ``None``.

    Locale-independent: inspects only numeric suffix codes and the
    group/unique flag bit -- there is no textual status field in NBNS to
    depend on. Fail-closed on any malformed/short/garbage shape."""
    try:
        if len(data) < 12 or int.from_bytes(data[6:8], "big") < 1:
            return None  # short header, or zero answer RRs
        off = 12
        name_len = data[off]
        off += 1 + name_len + 1  # length byte + encoded name + null terminator
        off += 2 + 2 + 4  # type + class + ttl
        if off + 2 > len(data):
            return None
        rdlen = int.from_bytes(data[off : off + 2], "big")
        off += 2
        if rdlen < 1 or off >= len(data):
            return None
        num = data[off]
        off += 1
        by_suffix: Dict[int, str] = {}
        for _ in range(num):
            entry = data[off : off + 18]
            off += 18
            if len(entry) < 18:
                break
            suffix = entry[15]
            is_group = bool(int.from_bytes(entry[16:18], "big") & _GROUP_FLAG)
            if is_group or suffix in by_suffix:
                continue
            nm = _clean_name(entry[:15])
            if nm:
                by_suffix[suffix] = nm
        for suffix in _SUFFIX_PRIORITY:
            if suffix in by_suffix:
                return by_suffix[suffix]
        return None
    except (IndexError, ValueError):
        return None


def _udp_socket() -> Any:
    return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def _rfc1918_targets(ips: Iterable[str], cap: int) -> List[str]:
    targets: List[str] = []
    seen: Set[str] = set()
    for ip in ips:
        if not ip or ip in seen or not _is_rfc1918(ip):
            continue
        seen.add(ip)
        targets.append(ip)
        if len(targets) >= max(0, cap):
            break
    return targets


def _drain(sock: Any, wanted: Set[str], deadline: float) -> Dict[str, str]:
    out: Dict[str, str] = {}
    stop_at = time.monotonic() + max(0.0, deadline)
    while (wanted - out.keys()) and time.monotonic() < stop_at:
        try:
            data, addr = sock.recvfrom(_BUFSIZE)
        except socket.timeout:
            break
        except OSError:
            break
        src = addr[0] if addr else ""
        if src not in wanted or src in out:
            continue
        name = _parse_node_status(data)
        if name:
            out[src] = name
    return out


def resolve_netbios_names(
    ips: Iterable[str],
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    cap: int = _DEFAULT_CAP,
    overall_deadline: float = _DEFAULT_DEADLINE,
    sock_factory: Callable[[], Any] = _udp_socket,
) -> Dict[str, str]:
    """NetBIOS names for RFC1918 ``ips``, resolved via an NBNS Node Status query.

    Unicasts one NBSTAT request per (deduped, RFC1918, capped) target, then
    drains replies until every target has answered or ``overall_deadline``
    elapses. Fail-closed: a non-responding or malformed host is simply absent
    from the result, never guessed. ``sock_factory`` is injectable so the
    suite never opens a real socket."""
    targets = _rfc1918_targets(ips, cap)
    if not targets:
        return {}
    try:
        sock = sock_factory()
    except OSError:
        return {}
    try:
        sock.settimeout(timeout)
        for ip in targets:
            with contextlib.suppress(OSError):
                sock.sendto(_QUERY, (ip, _NETBIOS_PORT))
        return _drain(sock, set(targets), overall_deadline)
    finally:
        with contextlib.suppress(OSError):
            sock.close()
