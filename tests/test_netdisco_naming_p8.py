"""Ф8 T2: reverse-DNS naming (low-priority hostname hint).

``socket.gethostbyaddr`` is the only stdlib reverse path; it blocks, so lookups
run capped, under a hard per-call timeout, RFC1918-only, results cached, and
ALWAYS fail-closed -- a missing/garbage PTR must never fabricate identity. The
resolver is injectable so the suite never touches the network. RED first.
"""

from __future__ import annotations

import socket

from server.netdisco import naming


def test_reverse_dns_returns_cleaned_name_for_rfc1918():
    # A resolver that hands back an FQDN with a trailing dot (as real PTR does).
    def fake(ip):
        assert ip == "10.0.0.5"
        return ("printer-3.office.local.", [], [ip])

    assert naming.reverse_dns("10.0.0.5", resolver=fake, cache={}) == "printer-3.office.local"


def test_reverse_dns_never_probes_public_ip():
    calls = []

    def fake(ip):
        calls.append(ip)
        return ("evil.example.com", [], [ip])

    assert naming.reverse_dns("8.8.8.8", resolver=fake, cache={}) is None
    assert calls == []  # the public address never reached the resolver


def test_reverse_dns_fail_closed_on_resolver_error():
    def boom(ip):
        raise socket.herror("no PTR")

    assert naming.reverse_dns("192.168.1.9", resolver=boom, cache={}) is None


def test_reverse_dns_rejects_control_chars_in_name():
    def fake(ip):
        return ("bad\x00name\n", [], [ip])

    assert naming.reverse_dns("172.16.0.4", resolver=fake, cache={}) is None


def test_reverse_dns_caches_negative_and_positive():
    calls = []
    cache: dict = {}

    def fake(ip):
        calls.append(ip)
        return ("host.lan.", [], [ip])

    assert naming.reverse_dns("10.1.1.1", resolver=fake, cache=cache) == "host.lan"
    assert naming.reverse_dns("10.1.1.1", resolver=fake, cache=cache) == "host.lan"
    assert calls == ["10.1.1.1"]  # second hit served from cache


def test_resolve_names_filters_rfc1918_and_caps():
    seen = []

    def fake(ip):
        seen.append(ip)
        return (f"h{ip[-1]}.lan", [], [ip])

    out = naming.resolve_names(
        ["10.0.0.1", "8.8.8.8", "192.168.0.2", "10.0.0.1"],  # public + dup dropped
        cap=2,
        resolver=fake,
        cache={},
    )
    # public never probed; dup collapsed; cap honoured (<=2 distinct RFC1918 probed)
    assert "8.8.8.8" not in out
    assert all(ip != "8.8.8.8" for ip in seen)
    assert len(seen) <= 2
    assert out.get("10.0.0.1") == "h1.lan"
