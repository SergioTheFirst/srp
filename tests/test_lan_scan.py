"""P2: agent-side bounded active LAN liveness sweep (behind active_scan).

lan_scan does TCP touches across the agent's OWN /24 so Windows ARP-resolves
live hosts; the existing Get-NetNeighbor read then surfaces them. No SNMP, no
configurable targets/ports (2026-06-11 spec G2). Every test injects the
``touch``/``host_check`` callable -- this suite never opens a real OS socket.
"""

from __future__ import annotations

import pytest
from client.collectors import lan_scan

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# _is_rfc1918                                                                  #
# --------------------------------------------------------------------------- #
def test_is_rfc1918_accepts_all_three_private_blocks():
    assert lan_scan._is_rfc1918("10.1.2.3")
    assert lan_scan._is_rfc1918("172.16.0.5")
    assert lan_scan._is_rfc1918("192.168.9.6")


def test_is_rfc1918_rejects_public_ipv6_and_garbage():
    assert not lan_scan._is_rfc1918("8.8.8.8")
    assert not lan_scan._is_rfc1918("172.32.0.1")  # just outside 172.16/12
    assert not lan_scan._is_rfc1918("::1")  # IPv6 loopback
    assert not lan_scan._is_rfc1918("fd00::1")  # IPv6 ULA
    assert not lan_scan._is_rfc1918("not-an-ip")
    assert not lan_scan._is_rfc1918("")


# --------------------------------------------------------------------------- #
# own_lan_cidrs                                                                #
# --------------------------------------------------------------------------- #
def test_own_lan_cidrs_derives_slash24_for_rfc1918():
    assert lan_scan.own_lan_cidrs(["192.168.9.6"]) == ["192.168.9.0/24"]


def test_own_lan_cidrs_drops_non_rfc1918():
    assert lan_scan.own_lan_cidrs(["8.8.8.8", "1.1.1.1"]) == []


def test_own_lan_cidrs_dedupes_same_segment():
    assert lan_scan.own_lan_cidrs(["192.168.9.6", "192.168.9.25"]) == ["192.168.9.0/24"]


def test_own_lan_cidrs_sorts_multiple_segments():
    assert lan_scan.own_lan_cidrs(["192.168.9.6", "10.0.0.5"]) == [
        "10.0.0.0/24",
        "192.168.9.0/24",
    ]


def test_own_lan_cidrs_empty_input():
    assert lan_scan.own_lan_cidrs([]) == []


# --------------------------------------------------------------------------- #
# expand_hosts                                                                 #
# --------------------------------------------------------------------------- #
def test_expand_hosts_enumerates_full_slash24():
    hosts = lan_scan.expand_hosts(["192.168.9.0/24"])
    assert len(hosts) == 254  # .1 - .254; network/broadcast excluded
    assert "192.168.9.1" in hosts
    assert "192.168.9.254" in hosts
    assert "192.168.9.0" not in hosts
    assert "192.168.9.255" not in hosts


def test_expand_hosts_respects_max_hosts_cap():
    assert len(lan_scan.expand_hosts(["192.168.9.0/24"], max_hosts=10)) == 10


def test_expand_hosts_dedupes_overlapping_cidrs():
    assert len(lan_scan.expand_hosts(["192.168.9.0/24", "192.168.9.0/24"])) == 254


def test_expand_hosts_skips_invalid_and_non_rfc1918_cidrs():
    assert lan_scan.expand_hosts(["not-a-cidr"]) == []
    assert lan_scan.expand_hosts(["8.8.8.0/24"]) == []  # public range never enumerated


def test_expand_hosts_max_hosts_zero_is_kill_switch():
    assert lan_scan.expand_hosts(["192.168.9.0/24"], max_hosts=0) == []


# --------------------------------------------------------------------------- #
# sweep_host -- injected ``touch`` spy, never a real socket                    #
# --------------------------------------------------------------------------- #
class _TouchSpy:
    """Records (ip, port) calls; per-port result is a bool or an Exception to
    raise (to prove one bad port doesn't abort the host)."""

    def __init__(self, results):
        self._results = results
        self.calls = []

    def __call__(self, ip, port, timeout):
        self.calls.append((ip, port))
        r = self._results.get(port, False)
        if isinstance(r, BaseException):
            raise r
        return r


def test_sweep_host_returns_true_on_first_open_port_and_stops():
    spy = _TouchSpy({80: False, 443: True})
    assert lan_scan.sweep_host("192.168.9.6", touch=spy) is True
    # stopped after 443 answered -- 445/3389/9100 never tried
    assert spy.calls == [("192.168.9.6", 80), ("192.168.9.6", 443)]


def test_sweep_host_all_closed_returns_false_and_tries_every_port():
    spy = _TouchSpy({})  # every port defaults False
    assert lan_scan.sweep_host("192.168.9.6", touch=spy) is False
    assert [p for _, p in spy.calls] == list(lan_scan._FIXED_PORTS)


def test_sweep_host_non_rfc1918_returns_false_without_touching():
    spy = _TouchSpy({80: True})
    assert lan_scan.sweep_host("8.8.8.8", touch=spy) is False
    assert spy.calls == []  # defense-in-depth: a public host is never probed


def test_sweep_host_one_port_raising_does_not_abort_the_host():
    spy = _TouchSpy({80: OSError("boom"), 443: True})
    assert lan_scan.sweep_host("192.168.9.6", touch=spy) is True
    assert ("192.168.9.6", 443) in spy.calls  # continued past the raising port


def test_sweep_host_all_ports_raising_returns_false():
    spy = _TouchSpy({p: OSError("boom") for p in lan_scan._FIXED_PORTS})
    assert lan_scan.sweep_host("192.168.9.6", touch=spy) is False


# --------------------------------------------------------------------------- #
# sweep -- injected ``host_check`` spy; the real ThreadPoolExecutor runs but   #
# never touches the network (mirrors server scan tests)                        #
# --------------------------------------------------------------------------- #
def test_sweep_checks_only_hosts_inside_the_derived_cidr():
    seen = []

    def host_check(ip):
        seen.append(ip)
        return False

    lan_scan.sweep(["192.168.9.6"], host_check=host_check)
    assert seen  # something was checked
    assert all(ip.startswith("192.168.9.") for ip in seen)
    assert "192.168.9.6" in seen


def test_sweep_returns_count_of_answering_hosts():
    def host_check(ip):
        return ip in ("192.168.9.10", "192.168.9.20")

    assert lan_scan.sweep(["192.168.9.6"], host_check=host_check) == 2


def test_sweep_respects_max_hosts():
    seen = []

    def host_check(ip):
        seen.append(ip)
        return False

    lan_scan.sweep(["192.168.9.6"], max_hosts=5, host_check=host_check)
    assert len(seen) == 5


def test_sweep_empty_lan_ips_returns_zero_without_spawning_a_pool():
    def boom(ip):
        raise AssertionError("no lan ips -> no pool, host_check never called")

    assert lan_scan.sweep([], host_check=boom) == 0


def test_sweep_non_rfc1918_lan_ips_returns_zero_without_pool():
    def boom(ip):
        raise AssertionError("no RFC1918 cidr -> no pool")

    assert lan_scan.sweep(["8.8.8.8", "1.1.1.1"], host_check=boom) == 0


def test_sweep_max_hosts_zero_kill_switch_returns_zero():
    def boom(ip):
        raise AssertionError("kill-switch -> no pool")

    assert lan_scan.sweep(["192.168.9.6"], max_hosts=0, host_check=boom) == 0
