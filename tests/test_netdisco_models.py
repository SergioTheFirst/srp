"""Phase 1: immutable netdisco models (NetDevice, NetInterface).

Immutability is an SRP invariant (CLAUDE.md §5): models are frozen so a stored
device can never be mutated in place. UNKNOWN fields are None / empty tuple,
never an invented value.
"""

from __future__ import annotations

import dataclasses

import pytest
from server.netdisco.models import NetDevice, NetInterface


def test_net_device_requires_only_nid_and_defaults_to_unknown_type() -> None:
    dev = NetDevice(nid="nd-mac-AA-BB-CC-DD-EE-FF")
    assert dev.nid == "nd-mac-AA-BB-CC-DD-EE-FF"
    assert dev.dev_type == "unknown"
    assert dev.ip is None
    assert dev.vendor is None
    assert dev.interfaces == ()
    assert dev.sources == ()


def test_net_device_is_frozen() -> None:
    dev = NetDevice(nid="nd-unknown")
    with pytest.raises(dataclasses.FrozenInstanceError):
        dev.ip = "10.0.0.1"


def test_net_interface_defaults_all_fields_to_none() -> None:
    iface = NetInterface()
    assert iface.if_index is None
    assert iface.name is None
    assert iface.oper_up is None


def test_net_device_carries_interfaces_as_a_tuple() -> None:
    iface = NetInterface(if_index=1, name="eth0", oper_up=True)
    dev = NetDevice(nid="nd-unknown", interfaces=(iface,))
    assert dev.interfaces[0].name == "eth0"
    assert dev.interfaces[0].oper_up is True
