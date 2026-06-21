"""Phase 6 -- device classification (§4.2), RED first.

``classify(profile, agent_macs)`` turns the raw probe signals into a device type
by determinative evidence only, in precedence order: agent > router > switch/ap >
printer > endpoint > unknown. UNKNOWN over a guessed type is an SRP invariant -- a
vendor-enterprise sysObjectID is NOT, by itself, a type (HP makes non-printers,
Cisco makes non-routers).
"""

from __future__ import annotations

from server.analytics.oui import normalize_mac
from server.netdisco import oids
from server.netdisco.classify import classify
from server.netdisco.models import DeviceProfile, NetInterface

_AGENT_MAC = normalize_mac("aa:bb:cc:dd:ee:ff")
_OTHER_MAC = normalize_mac("00:1b:44:11:3a:b7")


def _wireless_iface():
    return NetInterface(if_index=1, if_type=oids.IF_TYPE_IEEE80211)


def test_agent_mac_match_wins_over_everything():
    # Looks like a router too, but it is our own machine -> agent (highest priority).
    profile = DeviceProfile(ip="10.0.0.1", responded=True, ip_forwarding=True, macs=(_AGENT_MAC,))
    assert classify(profile, {_AGENT_MAC}) == "agent"


def test_ip_forwarding_classifies_router():
    profile = DeviceProfile(ip="10.0.0.1", responded=True, ip_forwarding=True)
    assert classify(profile, set()) == "router"


def test_bridge_with_fdb_classifies_switch():
    profile = DeviceProfile(ip="10.0.0.2", responded=True, bridge_address="brmac", has_fdb=True)
    assert classify(profile, set()) == "switch"


def test_bridge_with_fdb_and_wireless_iface_classifies_ap():
    profile = DeviceProfile(
        ip="10.0.0.3",
        responded=True,
        bridge_address="brmac",
        has_fdb=True,
        interfaces=(_wireless_iface(),),
    )
    assert classify(profile, set()) == "ap"


def test_bridge_without_fdb_is_not_a_switch():
    # dot1dBaseBridgeAddress alone (no forwarding entries) is not a confirmed switch.
    profile = DeviceProfile(
        ip="10.0.0.4",
        responded=True,
        bridge_address="brmac",
        has_fdb=False,
        macs=(_OTHER_MAC,),
    )
    assert classify(profile, set()) == "endpoint"


def test_router_precedence_over_switch():
    profile = DeviceProfile(
        ip="10.0.0.5",
        responded=True,
        ip_forwarding=True,
        bridge_address="brmac",
        has_fdb=True,
    )
    assert classify(profile, set()) == "router"


def test_printer_flag_classifies_printer():
    profile = DeviceProfile(ip="10.0.0.6", responded=True, is_printer=True)
    assert classify(profile, set()) == "printer"


def test_responded_host_with_mac_is_endpoint():
    profile = DeviceProfile(ip="10.0.0.7", responded=True, macs=(_OTHER_MAC,))
    assert classify(profile, set()) == "endpoint"


def test_snmp_mute_without_mac_is_unknown():
    profile = DeviceProfile(ip="10.0.0.8", responded=False)
    assert classify(profile, set()) == "unknown"


def test_snmp_mute_with_known_mac_is_endpoint():
    # The cycle injects the inventory MAC -> a silent host we have seen on the LAN.
    profile = DeviceProfile(ip="10.0.0.9", responded=False, macs=(_OTHER_MAC,))
    assert classify(profile, set()) == "endpoint"


def test_vendor_sysobjectid_alone_is_not_a_type():
    # Enterprise sysObjectID present, but NO determinative signal -> endpoint, never
    # a guessed router/switch (UNKNOWN-over-false-classification invariant).
    profile = DeviceProfile(
        ip="10.0.0.10",
        responded=True,
        sys_object_id="1.3.6.1.4.1.9.1.999",
        macs=(_OTHER_MAC,),
    )
    assert classify(profile, set()) == "endpoint"
