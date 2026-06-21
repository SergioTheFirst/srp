"""Phase 7 -- passive SNMP harvest of ARP + routing tables (RED first).

A fake session feeds canned table walks. ``harvest_arp`` recovers (ip, mac) from
ipNetToMediaPhysAddress (IP from the OID suffix, MAC from the value); ``harvest_
routes`` recovers (cidr, next_hop, ifindex) from the ipCidrRoute index. Both keep
ONLY RFC1918 results -- a public neighbour/next-hop is never emitted as a
candidate. Read-only, bounded, no ping.
"""

from __future__ import annotations

from server.analytics.oui import normalize_mac
from server.netdisco import harvest, oids


class FakeSession:
    def __init__(self, tables=None):
        self._tables = tables or {}
        self.walked: list[tuple[str, int]] = []

    def walk(self, base_oid, *, max_rows=512):
        self.walked.append((base_oid, max_rows))
        return dict(self._tables.get(base_oid, {}))


def test_harvest_oids_are_numeric():
    for oid in (oids.IP_NET_TO_MEDIA_PHYS, oids.IP_CIDR_ROUTE_IF_INDEX):
        assert oid and all(part.isdigit() for part in oid.split("."))


def test_harvest_arp_parses_ip_from_suffix_and_mac_from_value():
    mac_raw = bytes([0x00, 0x1B, 0x44, 0x11, 0x3A, 0xB7]).decode("latin-1")
    session = FakeSession(
        tables={
            oids.IP_NET_TO_MEDIA_PHYS: {
                f"{oids.IP_NET_TO_MEDIA_PHYS}.2.10.0.0.5": mac_raw,
            }
        }
    )
    result = harvest.harvest_arp(session)
    assert result == [("10.0.0.5", normalize_mac("00:1b:44:11:3a:b7"))]


def test_harvest_arp_drops_non_rfc1918_neighbours():
    session = FakeSession(
        tables={
            oids.IP_NET_TO_MEDIA_PHYS: {
                f"{oids.IP_NET_TO_MEDIA_PHYS}.2.8.8.8.8": "xxxxxx",
                f"{oids.IP_NET_TO_MEDIA_PHYS}.2.192.168.1.9": "yyyyyy",
            }
        }
    )
    ips = [ip for ip, _ in harvest.harvest_arp(session)]
    assert ips == ["192.168.1.9"]  # public neighbour dropped


def test_harvest_routes_parses_cidr_nexthop_ifindex():
    # index = dest(10.0.0.0).mask(255.255.255.0).tos(0).nexthop(10.0.0.1)
    oid = f"{oids.IP_CIDR_ROUTE_IF_INDEX}.10.0.0.0.255.255.255.0.0.10.0.0.1"
    session = FakeSession(tables={oids.IP_CIDR_ROUTE_IF_INDEX: {oid: 3}})
    result = harvest.harvest_routes(session)
    assert result == [("10.0.0.0/24", "10.0.0.1", 3)]


def test_harvest_routes_drops_non_rfc1918_next_hop():
    # default route via a public next-hop -> not a local candidate
    oid = f"{oids.IP_CIDR_ROUTE_IF_INDEX}.0.0.0.0.0.0.0.0.0.8.8.8.8"
    session = FakeSession(tables={oids.IP_CIDR_ROUTE_IF_INDEX: {oid: 1}})
    assert harvest.harvest_routes(session) == []


def test_harvest_walks_are_bounded():
    session = FakeSession()
    harvest.harvest_arp(session, max_rows=128)
    harvest.harvest_routes(session, max_rows=64)
    assert session.walked == [
        (oids.IP_NET_TO_MEDIA_PHYS, 128),
        (oids.IP_CIDR_ROUTE_IF_INDEX, 64),
    ]
