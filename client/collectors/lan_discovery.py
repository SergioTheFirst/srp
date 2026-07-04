"""Agent-side passive relay of LAN multicast discovery traffic (P1).

The server's own passive collectors (server/netdisco/passive.py) are useless
when the server is not L2-adjacent to the target LAN: mDNS/SSDP/WSD multicast
never crosses a router. The agent IS L2-adjacent. This module only LISTENS --
it never sends a query, so it adds zero new egress -- on each RFC1918-bearing
LAN/Wi-Fi adapter, and relays a capped raw capture per (protocol, source ip).
Parsing stays server-side in the existing passive.parse_mdns/parse_ssdp/
parse_wsd, so the wire-format parsing logic exists exactly once.

Privacy: only RFC1918 responders are kept; only the first packet per
(protocol, ip) is relayed (mirrors server passive.py's own per-IP dedup); the
raw payload is truncated to _MAX_HINT_RAW_BYTES before it ever leaves the box.
"""

from __future__ import annotations

import base64
import contextlib
import ipaddress
import socket
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

_GROUPS: Dict[str, Tuple[str, int]] = {
    "mdns": ("224.0.0.251", 5353),
    "ssdp": ("239.255.255.250", 1900),
    "wsd": ("239.255.255.250", 3702),
}

_LISTEN_SECONDS = 3.0  # shared wall-clock budget for all 3 protocols together
_POLL_SLEEP = 0.05
_BUFSIZE = 2048
_MAX_HINT_RAW_BYTES = 768  # base64 -> exactly 1024 chars (shared/schema.py max_length)
_MAX_HINTS = 128

# Spec privacy contract: only RFC1918 responders are ever relayed. Deliberately
# a local copy (not imported from network.py): network.py calls INTO this
# module, so the reverse import would cycle (mirrors lan_names.py's own copy).
_RFC1918 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)


def _is_rfc1918(ip: str) -> bool:
    try:
        return any(ipaddress.ip_address(ip) in net for net in _RFC1918)
    except ValueError:
        return False


def _open_listener(
    group: str,
    port: int,
    member_ips: List[str],
    *,
    socket_factory: Callable[[int, int], Any] = socket.socket,
) -> Optional[Any]:
    """One UDP socket bound to ``port``, joined to ``group`` on each of
    ``member_ips`` specifically -- never a wildcard join. This is the whole P1
    fix: an unscoped join lets the OS pick whichever adapter it likes for
    multicast membership, including a VPN tunnel whose own address can ALSO be
    RFC1918 (e.g. an Outline endpoint at 10.x.x.x), which would otherwise leak
    the join onto the tunnel instead of the real LAN. Fail-closed: any socket
    error at any step, or zero successful joins, yields ``None``."""
    try:
        sock = socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
    except OSError:
        return None
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", port))
    except OSError:
        with contextlib.suppress(OSError):
            sock.close()
        return None
    joined = 0
    group_bytes = socket.inet_aton(group)
    for ip in member_ips:
        try:
            sock.setsockopt(
                socket.IPPROTO_IP,
                socket.IP_ADD_MEMBERSHIP,
                group_bytes + socket.inet_aton(ip),
            )
            joined += 1
        except OSError:
            continue  # one bad adapter must not sink the join on the others
    if not joined:
        with contextlib.suppress(OSError):
            sock.close()
        return None
    sock.setblocking(False)
    return sock


def _try_recv(sock: Any) -> Optional[Tuple[bytes, str]]:
    try:
        data, addr = sock.recvfrom(_BUFSIZE)
    except OSError:  # covers BlockingIOError: no datagram queued right now
        return None
    return data, (addr[0] if addr else "")


def _capture(
    socks: Dict[Any, str],
    *,
    deadline: float,
    cap: int,
    now_fn: Callable[[], float],
    sleep_fn: Callable[[float], None],
) -> List[dict]:
    out: Dict[Tuple[str, str], dict] = {}
    while len(out) < cap and now_fn() < deadline:
        progressed = False
        for sock, source in socks.items():
            got = _try_recv(sock)
            if got is None:
                continue
            progressed = True
            data, ip = got
            if not ip or not _is_rfc1918(ip) or (source, ip) in out:
                continue
            out[(source, ip)] = {
                "ip": ip,
                "source": source,
                "data_b64": base64.b64encode(data[:_MAX_HINT_RAW_BYTES]).decode("ascii"),
            }
        if not progressed:
            sleep_fn(_POLL_SLEEP)
    return list(out.values())[:cap]


def collect_lan_discovery(
    adapter_ips: Iterable[str],
    *,
    budget_seconds: float = _LISTEN_SECONDS,
    cap: int = _MAX_HINTS,
    socket_factory: Callable[[int, int], Any] = socket.socket,
    now_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> List[dict]:
    """Passively relay raw mDNS/SSDP/WSD captures for server-side parsing.

    ``adapter_ips`` must already be this host's own RFC1918 LAN/Wi-Fi adapter
    addresses, never a tunnel adapter's (network.py's ``_lan_adapter_ips``
    computes this from data already collected there) -- P1's whole point is
    joining multicast ONLY on real LAN adapters. Never sends a probe (zero new
    egress); a device already speaking mDNS/SSDP/WSD self-announces on its own
    schedule, so a single cycle may catch nothing -- coverage accrues over
    repeated agent cycles, the same class as any other opportunistic hint in
    this codebase. All I/O is injectable so the test suite never opens a real
    socket or sleeps real wall-clock time.
    """
    members = sorted({ip for ip in adapter_ips if ip and _is_rfc1918(ip)})
    if not members:
        return []
    socks: Dict[Any, str] = {}
    try:
        for source, (group, port) in _GROUPS.items():
            sock = _open_listener(group, port, members, socket_factory=socket_factory)
            if sock is not None:
                socks[sock] = source
        if not socks:
            return []
        deadline = now_fn() + budget_seconds
        return _capture(socks, deadline=deadline, cap=cap, now_fn=now_fn, sleep_fn=sleep_fn)
    finally:
        for sock in socks:
            with contextlib.suppress(OSError):
                sock.close()
