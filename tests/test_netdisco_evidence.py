"""Phase 8 -- link-evidence collection from LLDP/CDP/FDB (RED first).

A fake session feeds canned table walks. ``collect_lldp`` / ``collect_cdp`` turn
neighbour MIBs into ``LinkEvidence`` (local node -> remote chassis/device-id);
``read_fdb`` recovers ``{bridge_port: {mac}}`` + ``{bridge_port: ifindex}`` from the
bridge forwarding DB. All parsing is fail-closed: malformed rows are skipped, never
raised, and a 6-octet chassis-id renders as a normalised MAC so the same neighbour
seen over LLDP and FDB can later merge.
"""

from __future__ import annotations

from server.analytics.oui import normalize_mac
from server.netdisco import evidence, oids
from server.netdisco.models import NetDevice, NetInterface


class FakeSession:
    def __init__(self, tables=None):
        self._tables = tables or {}
        self.walked: list[tuple[str, int]] = []

    def walk(self, base_oid, *, max_rows=512):
        self.walked.append((base_oid, max_rows))
        return dict(self._tables.get(base_oid, {}))


def _octet_str(*octets: int) -> str:
    return bytes(octets).decode("latin-1")


def test_evidence_oids_are_numeric():
    for oid in (
        oids.LLDP_REM_CHASSIS_ID,
        oids.LLDP_REM_PORT_ID,
        oids.CDP_CACHE_DEVICE_ID,
        oids.CDP_CACHE_DEVICE_PORT,
        oids.DOT1D_TP_FDB_PORT,
        oids.DOT1D_BASE_PORT_IF_INDEX,
        oids.DOT1D_STP_PORT_DESIGNATED_BRIDGE,
    ):
        assert oid and all(part.isdigit() for part in oid.split("."))


def test_collect_lldp_yields_high_confidence_neighbour():
    # index = TimeMark(0).LocalPortNum(3).RemIndex(1); chassis = 6-octet MAC
    chassis = _octet_str(0x00, 0x1B, 0x44, 0x11, 0x3A, 0xB7)
    session = FakeSession(
        tables={
            oids.LLDP_REM_CHASSIS_ID: {f"{oids.LLDP_REM_CHASSIS_ID}.0.3.1": chassis},
        }
    )
    result = evidence.collect_lldp("sw1", session)
    assert result == [
        evidence.LinkEvidence(
            a="sw1",
            b=normalize_mac("00:1b:44:11:3a:b7"),
            source=evidence.SOURCE_LLDP,
            confidence=evidence.HIGH,
            local_if=3,
        )
    ]


def test_collect_cdp_yields_high_confidence_neighbour():
    # index = cdpCacheIfIndex(3).cdpCacheDeviceIndex(1); value = remote device id
    session = FakeSession(
        tables={
            oids.CDP_CACHE_DEVICE_ID: {f"{oids.CDP_CACHE_DEVICE_ID}.3.1": "Switch2.corp"},
        }
    )
    result = evidence.collect_cdp("sw1", session)
    assert result == [
        evidence.LinkEvidence(
            a="sw1",
            b="Switch2.corp",
            source=evidence.SOURCE_CDP,
            confidence=evidence.HIGH,
            local_if=3,
        )
    ]


def test_read_fdb_groups_macs_by_bridge_port_with_ifindex():
    fdb_base = oids.DOT1D_TP_FDB_PORT
    pif_base = oids.DOT1D_BASE_PORT_IF_INDEX
    session = FakeSession(
        tables={
            fdb_base: {
                f"{fdb_base}.0.27.68.17.58.183": 5,  # mac on bridge port 5
                f"{fdb_base}.0.27.68.17.58.184": 5,  # second mac, same port
                f"{fdb_base}.222.173.190.239.0.1": 7,  # mac on bridge port 7
            },
            pif_base: {
                f"{pif_base}.5": 10,  # bridge port 5 -> ifIndex 10
                f"{pif_base}.7": 14,
            },
        }
    )
    port_macs, port_if = evidence.read_fdb(session)
    assert port_macs == {
        5: {normalize_mac("00:1b:44:11:3a:b7"), normalize_mac("00:1b:44:11:3a:b8")},
        7: {normalize_mac("de:ad:be:ef:00:01")},
    }
    assert port_if == {5: 10, 7: 14}


def test_collect_lldp_skips_malformed_rows():
    session = FakeSession(
        tables={
            oids.LLDP_REM_CHASSIS_ID: {
                f"{oids.LLDP_REM_CHASSIS_ID}.0.3": _octet_str(1, 2, 3, 4, 5, 6),  # short index
                "9.9.9.9": _octet_str(1, 2, 3, 4, 5, 6),  # not under prefix
                f"{oids.LLDP_REM_CHASSIS_ID}.0.4.1": "",  # empty chassis -> dropped
            }
        }
    )
    assert evidence.collect_lldp("sw1", session) == []


def test_collect_evidence_combines_sources_and_filters_own_mac():
    chassis = _octet_str(0x00, 0x1B, 0x44, 0x11, 0x3A, 0xB7)  # neighbour over LLDP
    host_a = normalize_mac("aa:11:22:33:44:55")  # a mute host on bridge port 3
    fdb_base = oids.DOT1D_TP_FDB_PORT
    pif_base = oids.DOT1D_BASE_PORT_IF_INDEX
    device = NetDevice(
        nid="sw1",
        mac="00-11-22-33-44-55",  # the switch's own MAC
        interfaces=(NetInterface(phys_mac=normalize_mac("00:11:22:33:44:66")),),
    )
    session = FakeSession(
        tables={
            oids.LLDP_REM_CHASSIS_ID: {f"{oids.LLDP_REM_CHASSIS_ID}.0.3.1": chassis},
            oids.CDP_CACHE_DEVICE_ID: {f"{oids.CDP_CACHE_DEVICE_ID}.2.1": "Switch2"},
            fdb_base: {
                f"{fdb_base}.170.17.34.51.68.85": 3,  # host_a -> edge link
                f"{fdb_base}.0.17.34.51.68.85": 4,  # the switch's own MAC -> filtered
            },
            pif_base: {f"{pif_base}.3": 10, f"{pif_base}.4": 11},
        }
    )
    result = evidence.collect_evidence(device, session)
    assert set(result) == {
        evidence.LinkEvidence(
            "sw1", normalize_mac("00:1b:44:11:3a:b7"), evidence.SOURCE_LLDP, evidence.HIGH, 3
        ),
        evidence.LinkEvidence("sw1", "Switch2", evidence.SOURCE_CDP, evidence.HIGH, 2),
        evidence.LinkEvidence("sw1", host_a, evidence.SOURCE_FDB_EDGE, evidence.HIGH, 10),
    }


def test_read_fdb_skips_malformed_octets_and_missing_port():
    fdb_base = oids.DOT1D_TP_FDB_PORT
    session = FakeSession(
        tables={
            fdb_base: {
                f"{fdb_base}.0.27.68.17.58.999": 5,  # octet out of range -> dropped
                f"{fdb_base}.0.27.68": 5,  # too few octets -> dropped
                "9.9.9.9.9.9.9": 5,  # not under prefix -> dropped
            }
        }
    )
    port_macs, port_if = evidence.read_fdb(session)
    assert port_macs == {}
    assert port_if == {}
