"""Ф7 T5: ifXTable enrichment. ``ifName`` (the short real port name, "Gi1/0/1")
is preferred over the verbose ``ifDescr`` for the interface name, and ``ifAlias``
(the operator description) is carried onto the interface. The classify cycle then
persists ``if_alias``. RED first.
"""

from __future__ import annotations

from server.netdisco import oids, scheduler, snmp_probe
from server.netdisco.models import DeviceProfile, NetInterface


class FakeSession:
    def __init__(self, tables=None):
        self._tables = tables or {}

    def get(self, oid_list):
        return {}

    def walk(self, base_oid, *, max_rows=512):
        return dict(self._tables.get(base_oid, {}))


def test_build_interfaces_prefers_ifname_and_sets_alias():
    s = FakeSession(
        {
            oids.IF_DESCR: {f"{oids.IF_DESCR}.1": "GigabitEthernet1/0/1"},  # verbose
            oids.IF_NAME: {f"{oids.IF_NAME}.1": "Gi1/0/1"},  # short, preferred
            oids.IF_ALIAS: {f"{oids.IF_ALIAS}.1": "uplink to core"},
        }
    )
    ifaces = snmp_probe._build_interfaces(s)
    assert len(ifaces) == 1
    assert ifaces[0].name == "Gi1/0/1"
    assert ifaces[0].if_alias == "uplink to core"


def test_build_interfaces_falls_back_to_ifdescr_without_ifname():
    s = FakeSession({oids.IF_DESCR: {f"{oids.IF_DESCR}.2": "eth2"}})
    ifaces = snmp_probe._build_interfaces(s)
    assert len(ifaces) == 1
    assert ifaces[0].name == "eth2"
    assert ifaces[0].if_alias is None


def test_build_interfaces_includes_ifname_only_index():
    # An interface that exposes only ifName (no ifDescr) is still listed.
    s = FakeSession({oids.IF_NAME: {f"{oids.IF_NAME}.7": "Te1/1/1"}})
    ifaces = snmp_probe._build_interfaces(s)
    assert [i.if_index for i in ifaces] == [7]
    assert ifaces[0].name == "Te1/1/1"


def test_iface_rows_carry_if_alias():
    prof = DeviceProfile(
        ip="10.0.0.1",
        interfaces=(NetInterface(if_index=1, name="Gi1/0/1", if_alias="uplink to core"),),
    )
    rows = scheduler._iface_rows(prof)
    assert rows[0]["if_alias"] == "uplink to core"


class _ProbeSession:
    def __init__(self, walks=None):
        self._w = walks or {}

    def get(self, oid_list):
        return {oids.SYS_NAME: "sw1"}  # non-empty -> responded=True

    def walk(self, base, *, max_rows=512):
        return dict(self._w.get(base, {}))


def test_probe_reads_entity_model_name():
    s = _ProbeSession(
        {oids.ENT_PHYSICAL_MODEL_NAME: {f"{oids.ENT_PHYSICAL_MODEL_NAME}.1": "Catalyst 2960"}}
    )
    prof = snmp_probe.probe_device("10.0.0.1", s)
    assert prof.model_name == "Catalyst 2960"


def test_probe_model_name_none_when_absent():
    prof = snmp_probe.probe_device("10.0.0.1", _ProbeSession())
    assert prof.model_name is None


def test_probe_drops_nonprintable_model_name():
    # A hostile device returning control bytes in the model is dropped (data hygiene).
    s = _ProbeSession(
        {oids.ENT_PHYSICAL_MODEL_NAME: {f"{oids.ENT_PHYSICAL_MODEL_NAME}.1": "Cat\x00\x01"}}
    )
    assert snmp_probe.probe_device("10.0.0.1", s).model_name is None


def test_build_interfaces_caps_if_alias_length():
    long_alias = "x" * 200
    s = _ProbeSession()  # not used; build directly below

    class _S:
        def walk(self, base, *, max_rows=512):
            if base == oids.IF_NAME:
                return {f"{oids.IF_NAME}.1": "Gi1/0/1"}
            if base == oids.IF_ALIAS:
                return {f"{oids.IF_ALIAS}.1": long_alias}
            return {}

    _ = s
    ifaces = snmp_probe._build_interfaces(_S())
    assert len(ifaces[0].if_alias) == 64


def test_device_update_prefers_entity_model_over_sysdescr():
    prof = DeviceProfile(
        ip="10.0.0.1",
        responded=True,
        sys_descr="Cisco IOS Software ...",
        model_name="Catalyst 2960",
    )
    upd = scheduler._device_update("nd-1", prof, "switch", {})
    assert upd["model"] == "Catalyst 2960"


def test_device_update_driver_model_still_wins():
    prof = DeviceProfile(ip="10.0.0.1", model_name="EntityModel")
    upd = scheduler._device_update("nd-1", prof, "switch", {"model": "DriverModel"})
    assert upd["model"] == "DriverModel"
