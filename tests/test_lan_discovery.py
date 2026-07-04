"""P1: agent-side passive relay of LAN multicast discovery traffic.

lan_discovery only LISTENS (never sends a query -- zero new egress) on each
RFC1918 LAN/Wi-Fi adapter, and relays a capped raw capture per (protocol,
source ip); parsing stays server-side (server/netdisco/passive.py). All
socket I/O and time are injected so this suite never opens a real socket or
sleeps real wall-clock time.
"""

from __future__ import annotations

import base64
import socket
from typing import Callable, List, Optional, Tuple

from client.collectors import lan_discovery

# --------------------------------------------------------------------------- #
# _is_rfc1918                                                                  #
# --------------------------------------------------------------------------- #


def test_is_rfc1918_accepts_all_three_private_blocks():
    assert lan_discovery._is_rfc1918("10.1.2.3")
    assert lan_discovery._is_rfc1918("172.16.0.5")
    assert lan_discovery._is_rfc1918("192.168.9.6")


def test_is_rfc1918_rejects_public_and_malformed():
    assert not lan_discovery._is_rfc1918("8.8.8.8")
    assert not lan_discovery._is_rfc1918("172.32.0.1")  # just outside 172.16/12
    assert not lan_discovery._is_rfc1918("not-an-ip")
    assert not lan_discovery._is_rfc1918("")


# --------------------------------------------------------------------------- #
# fakes -- this suite never opens a real socket or sleeps real time           #
# --------------------------------------------------------------------------- #


class FakeSocket:
    """Minimal non-blocking UDP socket double.

    ``recv_queue`` is popped in order; once exhausted, ``recvfrom`` raises
    ``BlockingIOError`` (a real non-blocking socket's "nothing queued right
    now"). ``join_fails`` names member IPs whose IP_ADD_MEMBERSHIP setsockopt
    raises OSError.
    """

    def __init__(
        self,
        recv_queue: Optional[List[Tuple[bytes, Tuple[str, int]]]] = None,
        *,
        bind_fails: bool = False,
        join_fails: Tuple[str, ...] = (),
    ):
        self.recv_queue = list(recv_queue or [])
        self.bind_fails = bind_fails
        self.join_fails = join_fails
        self.bound = None
        self.joined: List[bytes] = []
        self.closed = False
        self.blocking: Optional[bool] = None

    def setsockopt(self, level, optname, value=None):
        if level == socket.IPPROTO_IP and optname == socket.IP_ADD_MEMBERSHIP:
            member_ip = socket.inet_ntoa(value[4:8])
            if member_ip in self.join_fails:
                raise OSError("simulated join failure")
            self.joined.append(value)

    def bind(self, addr):
        if self.bind_fails:
            raise OSError("simulated bind failure")
        self.bound = addr

    def setblocking(self, flag):
        self.blocking = flag

    def recvfrom(self, bufsize):
        if not self.recv_queue:
            raise BlockingIOError("no datagram queued")
        return self.recv_queue.pop(0)

    def close(self):
        self.closed = True


def _factory_for(mdns: FakeSocket, ssdp: FakeSocket, wsd: FakeSocket) -> Callable[..., FakeSocket]:
    """A socket_factory returning one fake per call, in _GROUPS iteration
    order (mdns, ssdp, wsd -- a plain dict, so insertion order)."""
    order = [mdns, ssdp, wsd]
    calls = {"i": 0}

    def factory(*_args):
        sock = order[calls["i"]]
        calls["i"] += 1
        return sock

    return factory


class _FakeClock:
    """A zero-arg callable that advances by ``step`` each call -- lets the
    deadline loop terminate deterministically without a real sleep."""

    def __init__(self, start: float = 0.0, step: float = 1.0):
        self.value = start
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


# --------------------------------------------------------------------------- #
# _open_listener                                                               #
# --------------------------------------------------------------------------- #


def test_open_listener_joins_each_member_ip():
    sock = FakeSocket()
    out = lan_discovery._open_listener(
        "224.0.0.251", 5353, ["192.168.9.5", "192.168.9.6"], socket_factory=lambda *a: sock
    )
    assert out is sock
    assert len(sock.joined) == 2
    assert sock.blocking is False


def test_open_listener_returns_none_when_bind_fails():
    sock = FakeSocket(bind_fails=True)
    out = lan_discovery._open_listener(
        "224.0.0.251", 5353, ["192.168.9.5"], socket_factory=lambda *a: sock
    )
    assert out is None
    assert sock.closed


def test_open_listener_returns_none_when_socket_factory_raises():
    def factory(*_a):
        raise OSError("too many open files")

    out = lan_discovery._open_listener("224.0.0.251", 5353, ["192.168.9.5"], socket_factory=factory)
    assert out is None


def test_open_listener_one_bad_adapter_does_not_sink_the_others():
    sock = FakeSocket(join_fails=("192.168.9.5",))
    out = lan_discovery._open_listener(
        "224.0.0.251",
        5353,
        ["192.168.9.5", "192.168.9.6"],
        socket_factory=lambda *a: sock,
    )
    assert out is sock  # the second (good) join still succeeded
    assert len(sock.joined) == 1


def test_open_listener_returns_none_when_all_joins_fail():
    sock = FakeSocket(join_fails=("192.168.9.5", "192.168.9.6"))
    out = lan_discovery._open_listener(
        "224.0.0.251",
        5353,
        ["192.168.9.5", "192.168.9.6"],
        socket_factory=lambda *a: sock,
    )
    assert out is None
    assert sock.closed


# --------------------------------------------------------------------------- #
# _capture                                                                     #
# --------------------------------------------------------------------------- #


def test_capture_dedupes_by_source_and_ip_keeping_first():
    sock = FakeSocket([(b"first", ("192.168.9.5", 0)), (b"second", ("192.168.9.5", 0))])
    out = lan_discovery._capture(
        {sock: "mdns"}, deadline=10.0, cap=128, now_fn=_FakeClock(), sleep_fn=lambda s: None
    )
    assert len(out) == 1
    assert out[0]["data_b64"] == _b64(b"first")


def test_capture_drops_non_rfc1918_responder():
    sock = FakeSocket([(b"x", ("8.8.8.8", 0))])
    out = lan_discovery._capture(
        {sock: "mdns"}, deadline=10.0, cap=128, now_fn=_FakeClock(), sleep_fn=lambda s: None
    )
    assert out == []


def test_capture_enforces_cap():
    queue = [(f"p{i}".encode(), (f"192.168.9.{i}", 0)) for i in range(5)]
    sock = FakeSocket(queue)
    out = lan_discovery._capture(
        {sock: "mdns"}, deadline=10.0, cap=2, now_fn=_FakeClock(), sleep_fn=lambda s: None
    )
    assert len(out) == 2


def test_capture_truncates_raw_payload_to_max_hint_bytes():
    big = b"A" * (lan_discovery._MAX_HINT_RAW_BYTES + 500)
    sock = FakeSocket([(big, ("192.168.9.5", 0))])
    out = lan_discovery._capture(
        {sock: "mdns"}, deadline=10.0, cap=128, now_fn=_FakeClock(), sleep_fn=lambda s: None
    )
    decoded = base64.b64decode(out[0]["data_b64"])
    assert decoded == big[: lan_discovery._MAX_HINT_RAW_BYTES]


def test_capture_stops_at_deadline_via_injected_clock_not_real_sleep():
    """Never-arriving datagrams must not hang the collector -- the injected
    clock, not real wall time, ends the loop."""
    always_empty = FakeSocket()
    slept = {"n": 0}
    out = lan_discovery._capture(
        {always_empty: "mdns"},
        deadline=3.0,
        cap=128,
        now_fn=_FakeClock(start=0.0, step=1.0),
        sleep_fn=lambda s: slept.__setitem__("n", slept["n"] + 1),
    )
    assert out == []
    assert slept["n"] > 0  # it actually polled, not a busy-spin that never checked


def test_capture_tags_each_hint_with_its_own_socket_source():
    mdns_sock = FakeSocket([(b"m", ("192.168.9.6", 0))])
    ssdp_sock = FakeSocket([(b"s", ("192.168.9.7", 0))])
    out = lan_discovery._capture(
        {mdns_sock: "mdns", ssdp_sock: "ssdp"},
        deadline=10.0,
        cap=128,
        now_fn=_FakeClock(),
        sleep_fn=lambda s: None,
    )
    by_source = {h["source"]: h for h in out}
    assert by_source["mdns"]["ip"] == "192.168.9.6"
    assert by_source["ssdp"]["ip"] == "192.168.9.7"


# --------------------------------------------------------------------------- #
# collect_lan_discovery -- end to end, fake sockets throughout                #
# --------------------------------------------------------------------------- #


def test_collect_lan_discovery_empty_adapter_ips_opens_no_sockets():
    def factory(*_a):
        raise AssertionError("must not open a socket with no adapter ips")

    out = lan_discovery.collect_lan_discovery([], socket_factory=factory)
    assert out == []


def test_collect_lan_discovery_filters_non_rfc1918_member_ips():
    def factory(*_a):
        raise AssertionError("no RFC1918 member -> nothing to join")

    out = lan_discovery.collect_lan_discovery(["8.8.8.8", "1.1.1.1"], socket_factory=factory)
    assert out == []


def test_collect_lan_discovery_happy_path_relays_from_all_protocols():
    mdns_sock = FakeSocket([(b"mdns-payload", ("192.168.9.6", 5353))])
    ssdp_sock = FakeSocket([(b"ssdp-payload", ("192.168.9.7", 1900))])
    wsd_sock = FakeSocket([])  # nothing arrives on WSD this cycle
    factory = _factory_for(mdns_sock, ssdp_sock, wsd_sock)

    out = lan_discovery.collect_lan_discovery(
        ["192.168.9.5"],
        socket_factory=factory,
        now_fn=_FakeClock(step=1.0),
        sleep_fn=lambda s: None,
        budget_seconds=5.0,
    )

    by_source = {h["source"]: h for h in out}
    assert by_source["mdns"] == {
        "ip": "192.168.9.6",
        "source": "mdns",
        "data_b64": _b64(b"mdns-payload"),
    }
    assert by_source["ssdp"]["ip"] == "192.168.9.7"
    assert "wsd" not in by_source
    assert mdns_sock.closed and ssdp_sock.closed and wsd_sock.closed  # always cleaned up


def test_collect_lan_discovery_one_protocol_unavailable_others_still_work():
    mdns_sock = FakeSocket([(b"m", ("192.168.9.6", 0))])
    ssdp_sock = FakeSocket([(b"s", ("192.168.9.7", 0))])
    wsd_sock = FakeSocket(bind_fails=True)
    factory = _factory_for(mdns_sock, ssdp_sock, wsd_sock)

    out = lan_discovery.collect_lan_discovery(
        ["192.168.9.5"],
        socket_factory=factory,
        now_fn=_FakeClock(step=1.0),
        sleep_fn=lambda s: None,
        budget_seconds=5.0,
    )
    assert {h["source"] for h in out} == {"mdns", "ssdp"}


def test_collect_lan_discovery_all_listeners_fail_returns_empty():
    def factory(*_a):
        raise OSError("blocked by policy")

    out = lan_discovery.collect_lan_discovery(["192.168.9.5"], socket_factory=factory)
    assert out == []


def test_collect_lan_discovery_caps_total_hints():
    queue = [(f"p{i}".encode(), (f"192.168.9.{i}", 0)) for i in range(10)]
    mdns_sock = FakeSocket(queue)
    ssdp_sock = FakeSocket([])
    wsd_sock = FakeSocket([])
    factory = _factory_for(mdns_sock, ssdp_sock, wsd_sock)

    out = lan_discovery.collect_lan_discovery(
        ["192.168.9.5"],
        socket_factory=factory,
        now_fn=_FakeClock(step=1.0),
        sleep_fn=lambda s: None,
        budget_seconds=15.0,
        cap=3,
    )
    assert len(out) == 3
