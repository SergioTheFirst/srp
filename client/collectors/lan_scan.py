"""P2: agent-side bounded, read-only active liveness sweep of the agent's OWN
LAN segment (behind ``ClientConfig.active_scan``).

Why this exists / the ARP side-effect mechanism
------------------------------------------------
The agent is the only host L2-adjacent to the customer LAN; the server's own
active scan (``server/netdisco/scan.py``, ``server/printers/scan.py``) cannot
reach a segment the server is not itself on. This module gives the agent an
equivalent bounded sweep -- but it sends NO literal ARP frames. A bare TCP
connect attempt to a live host forces Windows to ARP-resolve that host's MAC as
a side effect, whether or not the probed port is open or the connection
succeeds. The network collector already reads the resulting ``Get-NetNeighbor``
table and runs it through the passive privacy/naming/relay pipeline, so this
sweep only needs to *populate* that table -- it emits no data of its own. The
``sweep`` return value is a best-effort "how many answered" count for logs and
never rides any wire payload; hence zero new schema fields.

Hard safety rails (2026-06-11 spec G1/G2; owner authorized active scanning of
owned RFC1918 segments in writing 2026-06-19, memory
``printer-active-scan-authorized``):
  * RFC1918 ONLY -- every derived CIDR and every enumerated/probed host is
    re-checked; a public address can never be touched.
  * Targets/ports NOT configurable -- CIDRs are only the agent's own /24(s),
    derived by the CALLER from lan/wifi-role adapter IPs (never a tunnel's), and
    the port list is fixed in code. There is deliberately no ``scan_cidrs``/
    ``scan_ports`` knob, so this stays a self-segment check, not a general
    scanner.
  * Bounded blast radius -- hosts capped (``max_hosts``, ``<= 0`` = kill-switch),
    worker fan-out capped, per-probe timeout short.
  * No SNMP -- unlike the server scanners this is TCP-connect only; the agent
    stays lightweight and SNMP lives server-side.

stdlib-only (socket / ipaddress / concurrent.futures) -- ``client/`` carries
zero third-party dependencies.
"""

from __future__ import annotations

import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

# 2026-06-11 spec G2: fixed, NOT configurable. Deliberately excludes 445/3389
# (SMB/RDP): security review flagged both as classic lateral-movement recon
# signatures that risk EDR self-quarantine fleet-wide, and neither is needed --
# the ARP side effect fires on ANY connect attempt regardless of port state
# (open, closed, or filtered), so there is no detection benefit to keeping them.
_FIXED_PORTS = (80, 443, 9100)
_TCP_TIMEOUT = 0.3
_MAX_WORKERS = 64
_MAX_HOSTS = 512  # defensive backstop (~2 /24s); a /24 is naturally <= 254 hosts

# Spec privacy contract: only RFC1918 targets are ever probed. A local copy, NOT
# imported from network.py -- every client/ module independently re-checks the
# privacy boundary (mirrors network.py's own _is_rfc1918_cidr "duplicated, not
# imported"), and network.py imports INTO this module, so the reverse would cycle.
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


def own_lan_cidrs(lan_ips: list[str]) -> list[str]:
    """The agent's own /24 segment(s), derived from its LAN/Wi-Fi adapter IPs.

    ``lan_ips`` MUST already be filtered to adapter role in {"lan","wifi"} by the
    caller (network.py::_lan_adapter_ips) -- a tunnel adapter's address, even an
    RFC1918 one (e.g. Outline's 10.0.85.2), must never seed a CIDR here, or the
    sweep would probe a remote network the operator may not own (the same
    VPN-leak gotcha P1 solved for its multicast join).
    """
    return sorted({ip.rsplit(".", 1)[0] + ".0/24" for ip in lan_ips if _is_rfc1918(ip)})


def expand_hosts(cidrs: list[str], max_hosts: int = _MAX_HOSTS) -> list[str]:
    """Enumerate RFC1918 host IPs across *cidrs*, deduped, capped at max_hosts.

    ``max_hosts <= 0`` is a kill-switch (returns ``[]``). Mirrors
    ``server/printers/scan.py::expand_cidrs``.
    """
    if max_hosts <= 0:
        return []  # 0 = kill-switch, never "one host" off the cap check
    out: list[str] = []
    seen: set[str] = set()
    for cidr in cidrs:
        try:
            net = ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            continue
        if not isinstance(net, ipaddress.IPv4Network):
            continue
        for host in net.hosts():
            ip = str(host)
            if ip in seen or not _is_rfc1918(ip):
                continue
            seen.add(ip)
            out.append(ip)
            if len(out) >= max_hosts:
                return out
    return out


def _tcp_touch(ip: str, port: int, timeout: float) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((ip, port)) == 0
    except OSError:
        return False
    finally:
        s.close()


def sweep_host(
    ip: str,
    *,
    ports: tuple[int, ...] = _FIXED_PORTS,
    timeout: float = _TCP_TIMEOUT,
    touch: Callable[[str, int, float], bool] = _tcp_touch,
) -> bool:
    """Touch *ip* on each fixed port until one answers.

    The point is the ARP side-effect (an open OR an actively-refused port both
    mean "the OS just ARP-resolved this host"); the return value is only a
    best-effort "did anything answer" signal for logs. RFC1918-rechecks *ip*
    first (defense in depth) and returns False WITHOUT calling ``touch`` at all
    for a non-RFC1918 address. One port raising must never abort the host -- each
    attempt is guarded and the next port is still tried.
    """
    if not _is_rfc1918(ip):
        return False
    for port in ports:
        try:
            if touch(ip, port, timeout):
                return True
        except Exception:  # noqa: BLE001 -- one bad port never aborts the host
            continue  # nosec B112 -- deliberate: try the next port, not a swallowed loop bug
    return False


def sweep(
    lan_ips: list[str],
    *,
    max_hosts: int = _MAX_HOSTS,
    max_workers: int = _MAX_WORKERS,
    host_check: Optional[Callable[[str], bool]] = None,
) -> int:
    """Touch every host on the agent's own /24(s) so Windows ARP-resolves the
    live ones; return the count that answered (for logs only -- never serialized).

    ``host_check`` is injectable so tests exercise enumeration/concurrency
    without touching the network. Empty ``lan_ips`` or an empty derived
    CIDR/host list returns 0 without spawning a pool.
    """
    hosts = expand_hosts(own_lan_cidrs(lan_ips), max_hosts)
    if not hosts:
        return 0
    check = host_check or sweep_host
    workers = min(max_workers, len(hosts))
    answered = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for ok in pool.map(check, hosts):
            if ok:
                answered += 1
    return answered
