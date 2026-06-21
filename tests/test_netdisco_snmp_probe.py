"""Phase 6 — SNMP device probe (RED first).

A fake SNMP session feeds canned scalars/tables so the probe is exercised with no
network. ``probe_device`` turns SNMP answers into an immutable ``DeviceProfile``:
system scalars, ipForwarding, bridge presence + FDB, interfaces (ifTable), device
MACs and a printer flag (reusing ``printers.classify.is_printer``). An unreachable
host (empty GET) costs nothing further -- no table walks at all.
"""

from __future__ import annotations

from server.analytics.oui import normalize_mac
from server.netdisco import oids, snmp_probe
from server.netdisco.models import DeviceProfile, NetInterface


class FakeSession:
    """Duck-typed SnmpSession: canned GET scalars + WALK tables, records calls."""

    def __init__(self, scalars=None, tables=None):
        self._scalars = scalars or {}
        self._tables = tables or {}  # base_oid -> {full_oid: value}
        self.walked: list[tuple[str, int]] = []

    def get(self, oid_list):
        return {o: self._scalars[o] for o in oid_list if o in self._scalars}

    def walk(self, base_oid, *, max_rows=512):
        self.walked.append((base_oid, max_rows))
        return dict(self._tables.get(base_oid, {}))


def _iftable(index, *, descr, if_type, speed_bps, phys, oper):
    """Build the {full_oid: value} entries for one ifTable row (one column each)."""
    return {
        f"{oids.IF_DESCR}.{index}": descr,
        f"{oids.IF_TYPE}.{index}": if_type,
        f"{oids.IF_SPEED}.{index}": speed_bps,
        f"{oids.IF_PHYS_ADDRESS}.{index}": phys,
        f"{oids.IF_OPER_STATUS}.{index}": oper,
    }


# --- oids: numeric / language-independent (SRP invariant) -------------------
def test_all_oids_are_numeric_dotted():
    values = [
        oids.SYS_DESCR,
        oids.SYS_OBJECT_ID,
        oids.SYS_NAME,
        oids.SYS_SERVICES,
        oids.IP_FORWARDING,
        oids.DOT1D_BASE_BRIDGE_ADDRESS,
        oids.DOT1D_TP_FDB_PORT,
        oids.IF_DESCR,
        oids.IF_TYPE,
        oids.IF_SPEED,
        oids.IF_PHYS_ADDRESS,
        oids.IF_OPER_STATUS,
        oids.ENT_PHYSICAL_SERIAL,
        oids.PRINTER_MIB,
    ]
    for oid in values:
        assert oid and all(part.isdigit() for part in oid.split("."))


# --- probe: unreachable pays nothing ---------------------------------------
def test_unreachable_host_returns_not_responded_and_walks_nothing():
    session = FakeSession(scalars={})  # GET answers nothing -> SNMP-mute
    profile = snmp_probe.probe_device("10.0.0.9", session)
    assert isinstance(profile, DeviceProfile)
    assert profile.responded is False
    assert profile.interfaces == ()
    assert profile.macs == ()
    assert session.walked == []  # no table walks on a dead host


# --- probe: scalars + routing ----------------------------------------------
def test_probe_parses_scalars_and_router_forwarding():
    session = FakeSession(
        scalars={
            oids.SYS_DESCR: "Vendor OS 1.0",
            oids.SYS_OBJECT_ID: "1.3.6.1.4.1.9.1.1",
            oids.SYS_NAME: "core-rtr",
            oids.SYS_SERVICES: 78,
            oids.IP_FORWARDING: 1,  # forwarding -> router signal
        }
    )
    profile = snmp_probe.probe_device("10.0.0.1", session)
    assert profile.responded is True
    assert profile.sys_object_id == "1.3.6.1.4.1.9.1.1"
    assert profile.sys_descr == "Vendor OS 1.0"
    assert profile.sys_name == "core-rtr"
    assert profile.sys_services == 78
    assert profile.ip_forwarding is True


def test_ip_forwarding_two_is_not_router():
    session = FakeSession(scalars={oids.SYS_DESCR: "host", oids.IP_FORWARDING: 2})
    profile = snmp_probe.probe_device("10.0.0.2", session)
    assert profile.ip_forwarding is False


def test_ip_forwarding_absent_is_unknown_none():
    session = FakeSession(scalars={oids.SYS_DESCR: "host"})
    profile = snmp_probe.probe_device("10.0.0.3", session)
    assert profile.ip_forwarding is None


# --- probe: interfaces (ifTable) -------------------------------------------
def test_probe_builds_interfaces_from_iftable():
    mac_raw = bytes([0x00, 0x1B, 0x44, 0x11, 0x3A, 0xB7]).decode("latin-1")
    tables = {
        oids.IF_DESCR: {},
        oids.IF_TYPE: {},
        oids.IF_SPEED: {},
        oids.IF_PHYS_ADDRESS: {},
        oids.IF_OPER_STATUS: {},
    }
    row = _iftable(
        2, descr="GigabitEthernet0/1", if_type=6, speed_bps=1_000_000_000, phys=mac_raw, oper=1
    )
    for full_oid, value in row.items():
        base = full_oid.rsplit(".", 1)[0]
        tables[base][full_oid] = value
    session = FakeSession(scalars={oids.SYS_DESCR: "sw"}, tables=tables)

    profile = snmp_probe.probe_device("10.0.0.4", session)

    assert len(profile.interfaces) == 1
    iface = profile.interfaces[0]
    assert isinstance(iface, NetInterface)
    assert iface.if_index == 2
    assert iface.name == "GigabitEthernet0/1"
    assert iface.if_type == 6
    assert iface.speed_mbps == 1000.0  # bps -> Mbps
    assert iface.oper_up is True
    assert iface.phys_mac == normalize_mac("00:1b:44:11:3a:b7")
    assert profile.macs == (normalize_mac("00:1b:44:11:3a:b7"),)


# --- probe: bridge + FDB (switch signals) ----------------------------------
def test_bridge_with_fdb_sets_has_fdb_true():
    session = FakeSession(
        scalars={oids.SYS_DESCR: "sw", oids.DOT1D_BASE_BRIDGE_ADDRESS: "bridgemac"},
        tables={oids.DOT1D_TP_FDB_PORT: {f"{oids.DOT1D_TP_FDB_PORT}.0.27.68.17.58.183": 5}},
    )
    profile = snmp_probe.probe_device("10.0.0.5", session)
    assert profile.bridge_address == "bridgemac"
    assert profile.has_fdb is True


def test_no_bridge_means_fdb_not_walked():
    session = FakeSession(scalars={oids.SYS_DESCR: "host"})
    profile = snmp_probe.probe_device("10.0.0.6", session)
    assert profile.bridge_address is None
    assert profile.has_fdb is False
    walked_bases = [base for base, _ in session.walked]
    assert oids.DOT1D_TP_FDB_PORT not in walked_bases  # skipped when no bridge


# --- probe: printer reuse + serial -----------------------------------------
def test_printer_mib_answer_sets_is_printer():
    session = FakeSession(
        scalars={oids.SYS_DESCR: "MFP"},
        tables={oids.PRINTER_MIB: {f"{oids.PRINTER_MIB}.5.1.1.17.1": "ABC123"}},
    )
    profile = snmp_probe.probe_device("10.0.0.7", session)
    assert profile.is_printer is True


def test_no_printer_mib_means_not_printer():
    session = FakeSession(scalars={oids.SYS_DESCR: "switch"})
    profile = snmp_probe.probe_device("10.0.0.8", session)
    assert profile.is_printer is False


def test_probe_extracts_entphysical_serial():
    session = FakeSession(
        scalars={oids.SYS_DESCR: "sw"},
        tables={oids.ENT_PHYSICAL_SERIAL: {f"{oids.ENT_PHYSICAL_SERIAL}.1": "FOC1234X5Y"}},
    )
    profile = snmp_probe.probe_device("10.0.0.10", session)
    assert profile.serial == "FOC1234X5Y"


# --- the bounded-walk seam the probe relies on -----------------------------
def test_snmp_session_walk_forwards_max_rows(monkeypatch):
    from server.printers import snmp as printer_snmp

    captured = {}

    def fake_walk(host, base_oid, **kwargs):
        captured["max_rows"] = kwargs.get("max_rows")
        return {}

    monkeypatch.setattr(printer_snmp, "snmp_walk", fake_walk)
    printer_snmp.SnmpSession("10.0.0.1").walk("1.3.6.1.2.1.2.2.1", max_rows=7)
    assert captured["max_rows"] == 7
