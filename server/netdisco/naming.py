"""Reverse-DNS naming for netdisco passive identification (Ф8 T2).

``socket.gethostbyaddr`` is the only stdlib reverse-DNS path and it blocks on the
system resolver, so lookups are bounded three ways: RFC1918-only (a public PTR is
never queried), a hard per-batch cap on the probe fan-out, and a thread-pool with
an overall deadline so a hung resolver can never stall a discovery cycle. Every
result is cached (positive AND negative) and the whole path is fail-closed -- a
missing or garbage PTR yields no name rather than a fabricated one.

The hostname produced here is a LOW-priority hint: a real agent/SNMP name always
wins (the assembler prefers those, and the writer only fills an empty field). The
resolver is injectable so the test-suite never touches the network.
"""

from __future__ import annotations

import concurrent.futures
import socket
from typing import Callable, Dict, Iterable, List, Optional, Set

from server.printers.discovery import is_rfc1918

_DEFAULT_TIMEOUT = 1.5  # seconds; per-batch overall budget is derived from this
_DEFAULT_WORKERS = 16  # bounded socket fan-out for the blocking resolver
_DEFAULT_CAP = 1024  # hard ceiling on hosts resolved per call (anti-blast)
_MAX_NAME = 253  # DNS name length ceiling

# Module-level cache shared across cycles. ``None`` is a negative result so a
# dead/PTR-less host is not re-queried every cycle. Tests pass their own dict.
_CACHE: Dict[str, Optional[str]] = {}

_Resolver = Callable[[str], "tuple[str, List[str], List[str]]"]


def _clean(name: Optional[str]) -> Optional[str]:
    """A PTR record, trimmed to a safe hostname, or ``None`` (fail-closed).

    Strips the trailing root dot, bounds the length, and accepts only hostname
    characters -- any control byte, whitespace or injection character means the
    record is discarded rather than trusted."""
    if not isinstance(name, str):
        return None
    host = name.strip().rstrip(".")
    if not host or len(host) > _MAX_NAME:
        return None
    if not all(c.isalnum() or c in "-._" for c in host):
        return None
    return host


def reverse_dns(
    ip: str,
    *,
    resolver: _Resolver = socket.gethostbyaddr,
    cache: Optional[Dict[str, Optional[str]]] = None,
) -> Optional[str]:
    """Cleaned reverse-DNS name for an RFC1918 ``ip``, else ``None``.

    Public addresses are rejected before the resolver is ever called; any
    resolver error (no PTR, timeout, malformed tuple) fails closed to ``None``.
    The result is cached so a repeat lookup is free."""
    if not ip or not is_rfc1918(ip):
        return None
    store = _CACHE if cache is None else cache
    if ip in store:
        return store[ip]
    name: Optional[str] = None
    try:
        result = resolver(ip)
        name = _clean(result[0])
    except (OSError, IndexError, TypeError, ValueError):
        name = None
    store[ip] = name
    return name


def resolve_names(
    ips: Iterable[str],
    *,
    cap: int = _DEFAULT_CAP,
    workers: int = _DEFAULT_WORKERS,
    timeout: float = _DEFAULT_TIMEOUT,
    resolver: _Resolver = socket.gethostbyaddr,
    cache: Optional[Dict[str, Optional[str]]] = None,
) -> Dict[str, str]:
    """Resolve a batch of IPs to hostnames, deduped, RFC1918-only and capped.

    Lookups run in a bounded thread-pool under an overall deadline; whatever has
    not resolved by then is dropped (fail-closed). Returns ``{ip: hostname}`` for
    the hosts that produced a clean name."""
    store = _CACHE if cache is None else cache
    targets: List[str] = []
    seen: Set[str] = set()
    for ip in ips:
        if not ip or ip in seen or not is_rfc1918(ip):
            continue
        seen.add(ip)
        targets.append(ip)
        if len(targets) >= max(0, cap):
            break
    out: Dict[str, str] = {}
    if not targets:
        return out
    overall = max(0.1, timeout) * 2 + 1.0
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        futs = {pool.submit(reverse_dns, ip, resolver=resolver, cache=store): ip for ip in targets}
        try:
            for fut in concurrent.futures.as_completed(futs, timeout=overall):
                name = fut.result()  # reverse_dns swallows all errors -> Optional[str]
                if name:
                    out[futs[fut]] = name
        except concurrent.futures.TimeoutError:
            pass  # overall budget exhausted; keep what completed (fail-closed)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return out
