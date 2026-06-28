"""Ф9e: NetFlow v9 / IPFIX collector adapter (identity from observed flows).

NetFlow is *push*, not poll: routers export flow records over UDP to a collector that
must listen continuously and cache the dynamic templates that arrive in between data
packets. So this adapter runs ONE bounded, fail-soft background receiver per endpoint;
``collect()`` just drains the identity observations it has accumulated.

Scope of this increment = **identity only**. A flow record yields an ``AdapterNode``
hint ONLY when the exporter includes a MAC field (``IN_SRC_MAC`` / ``OUT_DST_MAC`` in
v9, ``sourceMacAddress`` / ``destinationMacAddress`` in IPFIX) -- without a MAC there
is nothing to dedup/merge on (UNKNOWN over a guess), and the common IP-only case is
left to a later traffic-edge overlay (documented carry-forward; that layer needs
IP->nid resolution and is a separate map layer from physical topology).

Safety stance:
* ``collect()`` NEVER raises; the receiver loop never dies on a bad packet.
* Every observed IPv4 is RFC1918-gated -- a public peer never enters ``net_*``.
* The third-party ``netflow`` parser is *lazy-imported* (absent -> adapter fail-soft,
  the server still boots) and every call is wrapped (a malformed/unknown-template
  packet is skipped, never raised).
* Bounded everywhere: datagram size cap, a ring buffer (``max_buffer``) caps memory
  under a flood, the template cache is capped, the socket has a timeout.
* The receiver binds the operator-configured RFC1918 endpoint on UDP 2055 (no
  privileged port, no ``0.0.0.0`` wildcard); we only ever *receive*, never send
  (no reflection/amplification surface). The parser/socket are injectable for tests.
"""

from __future__ import annotations

import contextlib
import ipaddress
import logging
import socket
import threading
from collections import deque
from typing import Any, Callable, Deque, List, Optional, Tuple

from server.analytics.oui import normalize_mac
from server.netdisco.adapters.base import (
    AdapterConfig,
    AdapterNode,
    AdapterResult,
    NetworkAdapter,
)
from server.printers.discovery import is_rfc1918

_log = logging.getLogger("srp.netdisco")

_DEFAULT_PORT = 2055
_MAX_DATAGRAM = 65535  # a UDP datagram cannot exceed this
_MAX_BUFFER = 4096  # ring of recent identity observations (bounds memory under a flood)
_MAX_TEMPLATES = 256  # cap the dynamic-template cache per source family

# Candidate field names per flow record (v9 first, IPFIX aliases second).
_SRC_IP: Tuple[str, ...] = ("IPV4_SRC_ADDR", "sourceIPv4Address")
_DST_IP: Tuple[str, ...] = ("IPV4_DST_ADDR", "destinationIPv4Address")
_SRC_MAC: Tuple[str, ...] = ("IN_SRC_MAC", "sourceMacAddress", "postSourceMacAddress")
_DST_MAC: Tuple[str, ...] = (
    "OUT_DST_MAC",
    "IN_DST_MAC",
    "destinationMacAddress",
    "postDestinationMacAddress",
)

# parse(data, templates) -> packet with ``.flows`` (each flow has ``.data`` dict).
ParseFn = Callable[[bytes, Any], Any]

_UNSET: Any = object()
_PARSE: Any = _UNSET  # cached lazy netflow.parse_packet (or None if unavailable)
_RECEIVERS: dict[str, "FlowReceiver"] = {}  # one persistent receiver per endpoint
_RECEIVERS_LOCK = threading.Lock()  # serialize get-or-create so a race can't double-bind


# --- value normalisation -----------------------------------------------------


def _to_ip(value: Any) -> Optional[str]:
    """An RFC1918 IPv4 string from a flow value (str/int/4-or-16 bytes), or ``None``
    (public, IPv6, or garbage). A public/garbage peer never enters ``net_*``."""
    try:
        if isinstance(value, str):
            ip = ipaddress.ip_address(value.strip())
        elif isinstance(value, bool):  # bool is an int subclass -> reject explicitly
            return None
        elif isinstance(value, int):
            ip = ipaddress.ip_address(value)
        elif isinstance(value, (bytes, bytearray)) and len(value) in (4, 16):
            ip = ipaddress.ip_address(bytes(value))
        else:
            return None
    except (ValueError, OverflowError):
        return None
    if ip.version != 4:
        return None
    return ip.compressed if is_rfc1918(ip.compressed) else None


def _to_mac(value: Any) -> Optional[str]:
    """A canonical ``aa:bb:cc:dd:ee:ff`` MAC from a flow value (6 bytes / 48-bit int /
    string), or ``None`` when it is not a well-formed MAC."""
    if isinstance(value, (bytes, bytearray)):
        if len(value) != 6:
            return None
        mac = ":".join(f"{b:02x}" for b in value)
    elif isinstance(value, bool):
        return None
    elif isinstance(value, int):
        if value < 0 or value > 0xFFFFFFFFFFFF:
            return None
        mac = ":".join(f"{(value >> (8 * i)) & 0xFF:02x}" for i in range(5, -1, -1))
    elif isinstance(value, str):
        mac = value.strip()
        if not mac:
            return None
    else:
        return None
    norm = normalize_mac(mac)
    if not norm:
        return None
    hexonly = norm.replace("-", "").replace(":", "")
    # Drop a multicast/broadcast MAC (low bit of the first octet set) and the all-zero
    # MAC: neither is a real host NIC, and a hostile/garbage exporter could otherwise
    # seed junk `discovered` nodes from them.
    if int(hexonly[:2], 16) & 0x01 or set(hexonly) == {"0"}:
        return None
    return mac


def _first(data: dict, names: Tuple[str, ...]) -> Any:
    for name in names:
        if name in data and data[name] is not None:
            return data[name]
    return None


def _node(ip_raw: Any, mac_raw: Any) -> Optional[AdapterNode]:
    """One endpoint identity hint, or ``None`` when there is no MAC (nothing to merge
    on). The IP is optional (RFC1918 or dropped); the MAC is required."""
    mac = _to_mac(mac_raw)
    if not mac:
        return None
    return AdapterNode(mac=mac, ip=_to_ip(ip_raw), dev_type="endpoint")


def _identities_from_flow(data: dict) -> List[AdapterNode]:
    """Up to two endpoint identity hints (src, dst) from one flow record -- only the
    ends that carry a MAC field."""
    out: List[AdapterNode] = []
    src = _node(_first(data, _SRC_IP), _first(data, _SRC_MAC))
    if src is not None:
        out.append(src)
    dst = _node(_first(data, _DST_IP), _first(data, _DST_MAC))
    if dst is not None:
        out.append(dst)
    return out


# --- lazy parser + socket ----------------------------------------------------


def _lazy_parse() -> Optional[ParseFn]:
    """The ``netflow.parse_packet`` function, or ``None`` (package absent -> the
    adapter is fail-soft and the server still boots)."""
    global _PARSE
    if _PARSE is _UNSET:
        try:
            from netflow import parse_packet

            _PARSE = parse_packet
        except Exception:
            _PARSE = None
            _log.warning("flow: 'netflow' package unavailable; NetFlow adapter disabled")
    return _PARSE


def _udp_socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    return sock


# --- receiver ----------------------------------------------------------------


class FlowReceiver:
    """A bounded, fail-soft NetFlow receiver: ingest parses datagrams into identity
    hints in a ring buffer; ``drain`` returns and clears them. The parser is injected
    in tests (no socket, no real packets)."""

    def __init__(self, *, parse: Optional[ParseFn] = None, max_buffer: int = _MAX_BUFFER) -> None:
        self._parse = parse
        self._buf: Deque[AdapterNode] = deque(maxlen=max_buffer)
        # netflow's parse_packet caches dynamic templates here across packets (keyed by
        # template id). The library mutates this dict; we cap it in _guard_templates.
        self._templates: dict = {"netflow": {}, "ipfix": {}}
        self._lock = threading.Lock()
        self._sock: Optional[socket.socket] = None
        self._running = False

    def ingest(self, data: bytes) -> None:
        """Parse one datagram and buffer its identity hints. Never raises: an unknown
        template / malformed packet is skipped (fail-closed)."""
        parse = self._parse or _lazy_parse()
        if parse is None:
            return
        try:
            packet = parse(data, self._templates)
        except Exception:  # unknown-template / malformed -> skip, keep listening
            self._guard_templates()
            return
        nodes: List[AdapterNode] = []
        for flow in getattr(packet, "flows", None) or []:
            record = getattr(flow, "data", None)
            if isinstance(record, dict):
                nodes.extend(_identities_from_flow(record))
        if nodes:
            with self._lock:
                self._buf.extend(nodes)
        self._guard_templates()

    def drain(self) -> List[AdapterNode]:
        with self._lock:
            out = list(self._buf)
            self._buf.clear()
        return out

    def _guard_templates(self) -> None:
        """Cap the dynamic-template cache so a flood of distinct templates can't grow
        memory without bound (drops the oldest). Handles both the dict cache netflow
        0.12 uses and the list form older versions documented."""
        for key in ("netflow", "ipfix"):
            cache = self._templates.get(key)
            if isinstance(cache, dict) and len(cache) > _MAX_TEMPLATES:
                for tid in list(cache)[: len(cache) - _MAX_TEMPLATES]:
                    cache.pop(tid, None)
            elif isinstance(cache, list) and len(cache) > _MAX_TEMPLATES:
                del cache[: len(cache) - _MAX_TEMPLATES]

    def start(
        self,
        bind_ip: str,
        port: int = _DEFAULT_PORT,
        *,
        sock_factory: Optional[Callable[[], socket.socket]] = None,
    ) -> bool:
        """Bind the UDP collector and spawn the daemon receive loop. Returns False
        (fail-soft) if the bind fails -- the adapter then reports unavailable."""
        if self._running:
            return True
        try:
            sock = (sock_factory or _udp_socket)()
            sock.bind((bind_ip, port))  # operator RFC1918 endpoint, fixed NetFlow port
            sock.settimeout(1.0)
        except OSError:
            _log.warning("flow receiver: bind %s:%d failed", bind_ip, port)
            return False
        self._sock = sock
        self._running = True
        threading.Thread(target=self._loop, name="srp-netflow", daemon=True).start()
        return True

    def stop(self) -> None:
        self._running = False
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None

    def _loop(self) -> None:
        while self._running and self._sock is not None:
            try:
                data, addr = self._sock.recvfrom(_MAX_DATAGRAM)
            except socket.timeout:
                continue
            except OSError:
                break
            # NetFlow is unauthenticated by protocol design: accept exports only from an
            # RFC1918 source (a LAN exporter), dropping any spoofed off-LAN packet.
            if not (addr and is_rfc1918(addr[0])):
                continue
            try:
                self.ingest(data)
            except Exception:  # the listener must never die on one bad packet
                _log.exception("flow receiver: ingest error")


def _ensure_receiver(config: AdapterConfig) -> Optional[FlowReceiver]:
    """The persistent receiver for this endpoint, started on first use. ``None`` when
    the ``netflow`` package is missing, the endpoint is not RFC1918, or the bind fails
    (all fail-soft). The receiver (a daemon thread + UDP socket) lives until process
    exit -- there is no per-cycle teardown (NetFlow is a continuous push source)."""
    if _lazy_parse() is None:
        return None
    if not is_rfc1918(config.endpoint):
        return None  # defense-in-depth: never bind off a non-private endpoint
    # Lock the get-or-create so two concurrent cycles can't both bind the same port
    # (production already serializes collect() under _poll_lock; this makes it self-safe).
    with _RECEIVERS_LOCK:
        existing = _RECEIVERS.get(config.endpoint)
        if existing is not None:
            return existing
        receiver = FlowReceiver()
        if not receiver.start(config.endpoint):
            return None
        _RECEIVERS[config.endpoint] = receiver
        return receiver


# --- adapter -----------------------------------------------------------------


class FlowAdapter(NetworkAdapter):
    """Drains the NetFlow receiver's accumulated identity hints. Inject ``receiver``
    in tests; in production a persistent receiver is started lazily on first use."""

    def __init__(
        self,
        config: AdapterConfig,
        *,
        store: Any = None,
        receiver: Optional[FlowReceiver] = None,
    ) -> None:
        super().__init__(config)
        self._receiver = receiver

    def collect(self) -> AdapterResult:
        try:
            receiver = self._receiver or _ensure_receiver(self.config)
            if receiver is None:
                return AdapterResult(errors=("flow: collector unavailable",))
            return AdapterResult(nodes=tuple(receiver.drain()))
        except Exception:  # collect() must NEVER raise -- one bad adapter can't break the cycle
            _log.exception("flow adapter failed for %s", self.config.endpoint)
            return AdapterResult(errors=("flow: unexpected error",))
