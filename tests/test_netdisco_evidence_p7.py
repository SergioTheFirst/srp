"""Ф7 evidence extensions: LLDP port-id (directed a_if/b_if labels), LLDP mgmt-addr
seed, LLDP-MED device class. RED first.

``lldp_loc_port_ifnames`` resolves the local LLDP port number to its ifIndex-via-name
so a directed port<->port link can show both ends' labels. ``collect_lldp_mgmt``
recovers the neighbour's management address (seed set, no ping). ``collect_lldp_med``
maps the LLDP-MED device class to a stable subtype label.
"""

from __future__ import annotations

import ipaddress

from server.analytics.oui import normalize_mac
from server.netdisco import evidence, oids
from server.netdisco.models import NetDevice


class FakeSession:
    def __init__(self, tables=None):
        self._tables = tables or {}

    def walk(self, base_oid, *, max_rows=512):
        return dict(self._tables.get(base_oid, {}))


def _octet_str(*octs: int) -> str:
    return bytes(octs).decode("latin-1")


def test_lldp_loc_port_ifnames_maps_port_num_to_name():
    # lldpLocPortDesc keyed by lldpLocPortNum -> the textual port description.
    session = FakeSession(
        tables={
            oids.LLDP_LOC_PORT_DESC: {
                f"{oids.LLDP_LOC_PORT_DESC}.3": "GigabitEthernet1/0/3",
                f"{oids.LLDP_LOC_PORT_DESC}.4": "Gi1/0/4",
            }
        }
    )
    assert evidence.lldp_loc_port_ifnames(session) == {
        3: "GigabitEthernet1/0/3",
        4: "Gi1/0/4",
    }


def test_lldp_loc_port_ifnames_skips_malformed():
    session = FakeSession(
        tables={
            oids.LLDP_LOC_PORT_DESC: {
                f"{oids.LLDP_LOC_PORT_DESC}.3": "Gi1/0/3",
                f"{oids.LLDP_LOC_PORT_DESC}.x": "Garbage",  # non-numeric port
                f"{oids.LLDP_LOC_PORT_DESC}.4": "",  # empty -> dropped
            }
        }
    )
    assert evidence.lldp_loc_port_ifnames(session) == {3: "Gi1/0/3"}


def test_collect_lldp_mgmt_yields_neighbour_seed_ips():
    # lldpRemManAddrTable: the management address rides in the OID index per the
    # LldpManAddressInfo TC -- index = TimeMark.LocalPortNum.RemIndex.AddrSubtype.
    # [AddrLen].<address octets>. For IPv4 (subtype 1) that is 4 octets at the tail.
    base = oids.LLDP_REM_MAN_ADDR
    session = FakeSession(tables={base: {f"{base}.0.3.1.1.4.192.168.1.7": ""}})
    seeds = evidence.collect_lldp_mgmt("sw1", session)
    assert seeds == [("sw1", "192.168.1.7")]


def test_collect_lldp_mgmt_drops_non_ipv4_and_garbage():
    base = oids.LLDP_REM_MAN_ADDR
    ipv6_octets = ".".join("0" for _ in range(16))
    session = FakeSession(
        tables={
            base: {
                # subtype 2 = IPv6 (16 octets) -> dropped (we seed IPv4 only)
                f"{base}.0.3.1.2.16." + ipv6_octets: "",
                f"{base}.0.3": "",  # too few index parts -> dropped
                # valid IPv4 (4 trailing octets)
                f"{base}.0.3.1.1.4.10.0.0.1": "",
                f"{base}.0.4.1.1.4.10.0.0.2": "",  # second neighbour on port 4
            }
        }
    )
    seeds = evidence.collect_lldp_mgmt("sw1", session)
    assert sorted(seeds) == sorted([("sw1", "10.0.0.1"), ("sw1", "10.0.0.2")])
    for _local, ip in seeds:
        ipaddress.ip_address(ip)


def test_collect_lldp_mgmt_drops_public_ip():
    # A hostile/foreign neighbour advertising a PUBLIC mgmt address must never become
    # a seed (defense-in-depth: the seed expansion stays RFC1918-only, fail-safe).
    base = oids.LLDP_REM_MAN_ADDR
    session = FakeSession(
        tables={
            base: {
                f"{base}.0.3.1.1.4.8.8.8.8": "",  # public -> dropped
                f"{base}.0.3.1.1.4.10.0.0.5": "",  # private -> kept
            }
        }
    )
    assert evidence.collect_lldp_mgmt("sw1", session) == [("sw1", "10.0.0.5")]


def test_collect_lldp_med_maps_class_to_subtype():
    base = oids.LLDP_XMED_REM_DEVICE_CLASS
    session = FakeSession(
        tables={
            base: {
                f"{base}.0.3.1": oids.LLDP_MED_PHONE,  # 10 -> phone (port 3)
                f"{base}.0.4.1": oids.LLDP_MED_AP,  # 6 -> ap (port 4)
                f"{base}.0.5.1": 99,  # unknown class -> omitted (no guess)
            }
        }
    )
    out = evidence.collect_lldp_med("sw1", session)
    # keyed by the local LLDP port number -> subtype label
    assert out == {3: "phone", 4: "ap"}


def test_collect_lldp_med_empty_when_absent():
    session = FakeSession(tables={})
    assert evidence.collect_lldp_med("sw1", session) == {}


def test_collect_lldp_sets_directed_port_labels():
    # lldpRemPortId shares the lldpRem index (TimeMark.LocalPortNum.RemIndex) with
    # lldpRemChassisId; the local port label comes from the passed loc_ports map.
    chassis = oids.LLDP_REM_CHASSIS_ID
    remport = oids.LLDP_REM_PORT_ID
    mac_bytes = _octet_str(0xAA, 0xBB, 0xCC, 0x00, 0x11, 0x22)
    session = FakeSession(
        tables={
            chassis: {f"{chassis}.0.3.1": mac_bytes},  # local port number = 3
            remport: {f"{remport}.0.3.1": "Gi0/1"},  # remote port label
        }
    )
    evs = evidence.collect_lldp("sw1", session, loc_ports={3: "Gi1/0/3"})
    assert len(evs) == 1
    ev = evs[0]
    assert ev.b == normalize_mac("AA-BB-CC-00-11-22")
    assert ev.a_port == "Gi1/0/3"  # local port label (this switch's port)
    assert ev.b_port == "Gi0/1"  # remote port label (the neighbour's port)


def test_collect_lldp_ports_none_without_data():
    # No rem-port / loc_ports -> ports stay None (UNKNOWN over a fabricated label).
    chassis = oids.LLDP_REM_CHASSIS_ID
    mac_bytes = _octet_str(0xAA, 0xBB, 0xCC, 0x00, 0x11, 0x22)
    session = FakeSession(tables={chassis: {f"{chassis}.0.3.1": mac_bytes}})
    evs = evidence.collect_lldp("sw1", session)
    assert len(evs) == 1
    assert evs[0].a_port is None
    assert evs[0].b_port is None


def test_read_fdb_dot1q_groups_macs_and_vlans():
    # dot1qTpFdbPort index = dot1qFdbId(vlan).mac6octets ; value = bridge port.
    base = oids.DOT1Q_TP_FDB_PORT
    session = FakeSession(
        tables={
            base: {
                f"{base}.10.0.27.68.17.58.183": 5,  # vlan 10, 00:1b:44:11:3a:b7 -> port 5
                f"{base}.20.170.187.204.0.17.34": 6,  # vlan 20, aa:bb:cc:00:11:22 -> port 6
                f"{base}.10.1.0.94.0.0.1": 7,  # multicast-ish first octet odd -> kept here (raw)
            }
        }
    )
    port_macs, mac_vlan = evidence.read_fdb_dot1q(session)
    assert port_macs[5] == {normalize_mac("00:1b:44:11:3a:b7")}
    assert port_macs[6] == {normalize_mac("aa:bb:cc:00:11:22")}
    assert mac_vlan[normalize_mac("00:1b:44:11:3a:b7")] == 10
    assert mac_vlan[normalize_mac("aa:bb:cc:00:11:22")] == 20


def test_read_fdb_dot1q_skips_malformed():
    base = oids.DOT1Q_TP_FDB_PORT
    session = FakeSession(
        tables={
            base: {
                f"{base}.10.0.27.68.17.58": 5,  # 5 octets (not a full MAC) -> dropped
                f"{base}.10.0.27.68.17.58.183": 0,  # port 0 (< 1) -> dropped
            }
        }
    )
    port_macs, mac_vlan = evidence.read_fdb_dot1q(session)
    assert port_macs == {}
    assert mac_vlan == {}


def test_collect_evidence_attaches_vlan_from_dot1q():
    # dot1q FDB places one host behind a port -> a HIGH edge tagged with its VLAN.
    fdb = oids.DOT1Q_TP_FDB_PORT
    pif = oids.DOT1D_BASE_PORT_IF_INDEX
    host = normalize_mac("00:1b:44:11:3a:b7")
    session = FakeSession(
        tables={
            fdb: {f"{fdb}.10.0.27.68.17.58.183": 5},
            pif: {f"{pif}.5": 10},
        }
    )
    evs = evidence.collect_evidence(NetDevice(nid="sw1"), session)
    edge = next(e for e in evs if e.source == evidence.SOURCE_FDB_EDGE)
    assert edge.b == host
    assert edge.vlan == 10
    assert edge.local_if == 10  # ifIndex resolved via dot1dBasePortIfIndex
