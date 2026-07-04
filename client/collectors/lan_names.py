"""Agent-side NetBIOS naming of LAN neighbors (client, additive, T2).

The agent is the only host L2-adjacent to a remote site's LAN, so it is the
only vantage point that can name those neighbors -- NBNS (UDP/137) does not
route off-subnet, and the server's passive NetBIOS collector
(``server/netdisco/passive.py::collect_netbios``) cannot reach a remote site.

This module used to speak NBNS directly over a user-space UDP socket. Live
debugging on a real Windows box proved that unreliable: the NetBT system
service owns UDP/137, and a user-space socket querying even the box's OWN
registered name (confirmed present via ``nbtstat -n``: FPLUS <00>/<20>)
received no reply. The Windows-native ``nbtstat -A <ip>`` tool DOES get
answers -- live-verified against real LAN hosts (192.168.9.6->MEDPOST,
.25->SKPD3, .100->I3). The agent is always Windows, so ``nbtstat`` is always
available; this module now shells out to it instead of the raw socket.

Safety invariants (mirrors the server passive collectors):
  * RFC1918-only -- a public IP is never queried (the agent's privacy contract);
  * bounded fan-out (``cap``) on a capped thread pool, plus a per-call
    subprocess timeout AND an overall wall-clock deadline, so a hung/slow
    segment can never stall the collector;
  * fail-closed -- a non-responding, erroring, or malformed reply yields NO
    name, never a fabricated one;
  * locale-independent -- the parser reads only the numeric ``<20>`` suffix
    marker; the UNIQUE/GROUP/Registered words beside it localize (observed as
    mojibake on a Russian console) and are never inspected.

``runner`` is injectable so the test-suite never spawns a real process.
"""

from __future__ import annotations

import ipaddress
import subprocess  # nosec B404 -- fixed argv, no shell; see _run_nbtstat
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Callable, Dict, Iterable, List, Optional, Set

from client.winflags import NO_WINDOW

_MAX_NETBIOS_NAME = 15  # NetBIOS name length ceiling (protocol max, significant chars)
_SUFFIX_SERVER = "<20>"  # Server-service suffix -- by NetBIOS convention always UNIQUE

_DEFAULT_CAP = 64  # hard ceiling on hosts queried per collection
_DEFAULT_TIMEOUT = 3.0  # per-host `nbtstat -A` subprocess timeout (seconds)
_DEFAULT_WORKERS = 16  # bounded thread-pool fan-out (nbtstat blocks ~1-3s per call)
_DEFAULT_DEADLINE = 15.0  # overall wall-clock budget for the whole batch (seconds)

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


def _clean_name(text: str) -> Optional[str]:
    """Allowlist a candidate NetBIOS name: alnum + ``-._``, non-empty, <=15 chars."""
    text = text.strip()
    if not text or len(text) > _MAX_NETBIOS_NAME or "*" in text:
        return None
    if not all(c.isalnum() or c in "-._" for c in text):
        return None
    return text


def _parse_nbtstat(text: str) -> Optional[str]:
    """The unique machine name from ``nbtstat -A`` output, else ``None``.

    Locale-independent: reads ONLY the name field preceding the literal
    ``<20>`` suffix marker on its line -- the Server-service record, which by
    NetBIOS convention is always a UNIQUE name, never a group. The
    UNIQUE/GROUP/Registered words that follow on the same line localize on
    non-English consoles and are never inspected. Fail-closed: no ``<20>``
    line (covers empty output and "Host not found") or an unclean name -> None.
    """
    for line in text.splitlines():
        idx = line.find(_SUFFIX_SERVER)
        if idx == -1:
            continue
        name = _clean_name(line[:idx])
        if name:
            return name
    return None


def _run_nbtstat(ip: str, *, timeout: float) -> str:
    """Default runner: ``nbtstat -A <ip>`` -> decoded stdout, "" on any failure.

    Argument list (never ``shell=True``), a bounded timeout, and no console
    window; ``ip`` is always RFC1918-validated by the caller before this runs."""
    try:
        proc = subprocess.run(  # nosec B603 B607 -- fixed argv, ip pre-validated RFC1918
            ["nbtstat", "-A", ip],
            capture_output=True,
            timeout=timeout,
            creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.decode("utf-8", errors="replace")


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


def resolve_netbios_names(
    ips: Iterable[str],
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    cap: int = _DEFAULT_CAP,
    overall_deadline: float = _DEFAULT_DEADLINE,
    max_workers: int = _DEFAULT_WORKERS,
    runner: Optional[Callable[[str], str]] = None,
) -> Dict[str, str]:
    """NetBIOS names for RFC1918 ``ips``, resolved via ``nbtstat -A`` per host.

    Runs one ``nbtstat -A <ip>`` per (deduped, RFC1918, capped) target on a
    bounded thread pool -- fanning the ~1-3s-per-call system tool out instead
    of serializing it. ``overall_deadline`` bounds the whole batch's wall
    clock: once it elapses, no further results are waited on (whatever
    already finished is kept) and any still-pending work is abandoned, so a
    slow/hung segment can never stall the collector. Fail-closed throughout:
    a non-responding, erroring, or malformed host is simply absent from the
    result, never guessed. ``runner`` is injectable so the suite never spawns
    a real process."""
    targets = _rfc1918_targets(ips, cap)
    if not targets:
        return {}
    call: Callable[[str], str] = (
        runner if runner is not None else (lambda ip: _run_nbtstat(ip, timeout=timeout))
    )

    out: Dict[str, str] = {}
    pool = ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(targets))))
    try:
        futures = {pool.submit(call, ip): ip for ip in targets}
        for future in as_completed(futures, timeout=overall_deadline):
            ip = futures[future]
            try:
                text = future.result()
            except Exception:  # nosec B112 -- one raising/hung host must never abort the batch
                continue
            name = _parse_nbtstat(text)
            if name:
                out[ip] = name
    except FuturesTimeoutError:
        pass  # overall deadline hit -- keep whatever resolved, abandon the rest
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return out
