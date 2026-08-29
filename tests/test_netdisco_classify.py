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


def _eth_ifaces(count, start_index=1):
    return tuple(
        NetInterface(if_index=start_index + i, if_type=oids.IF_TYPE_ETHERNET) for i in range(count)
    )


def test_agent_mac_match_wins_over_everything():
    # Looks like a router too, but it is our own machine -> agent (highest priority).
    profile = DeviceProfile(ip="10.0.0.1", responded=True, ip_forwarding=True, macs=(_AGENT_MAC,))
    assert classify(profile, {_AGENT_MAC}) == "agent"


def test_ip_forwarding_classifies_router():
    # o5-A2 conscious contract change: ip_forwarding ALONE no longer classifies a
    # router (a Docker/Hyper-V/ICS host sets ipForwarding=1 too). The router rule
    # now also requires the sysServices L3 bit and >=2 Ethernet ports; this test
    # was updated to supply that corroboration instead of asserting on the bit alone.
    profile = DeviceProfile(
        ip="10.0.0.1",
        responded=True,
        ip_forwarding=True,
        sys_services=oids.SYS_SERVICES_L3,
        interfaces=_eth_ifaces(2),
    )
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


def test_bridge_address_alone_is_switch():
    # o5-A2 conscious contract change: was "test_bridge_without_fdb_is_not_a_switch",
    # asserting "endpoint" -- the old rule required bridge_address AND has_fdb (both).
    # The new rule is `has_fdb OR bridge_address`: dot1dBaseBridgeAddress presence is
    # itself a confirmed L2-bridge signal (an idle/just-booted switch can have an
    # empty FDB), so bridge_address alone now classifies as switch.
    profile = DeviceProfile(
        ip="10.0.0.4",
        responded=True,
        bridge_address="brmac",
        has_fdb=False,
        macs=(_OTHER_MAC,),
    )
    assert classify(profile, set()) == "switch"


def test_router_precedence_over_switch():
    # o5-A2 conscious contract change: the router rule now needs sysServices L3 +
    # >=2 Ethernet ports as corroboration (see test_ip_forwarding_classifies_router);
    # this test was updated to supply it so it still proves router precedence over
    # the switch/bridge rule below it, rather than pinning the old bare-ip_forwarding
    # router rule.
    profile = DeviceProfile(
        ip="10.0.0.5",
        responded=True,
        ip_forwarding=True,
        sys_services=oids.SYS_SERVICES_L3,
        interfaces=_eth_ifaces(2),
        bridge_address="brmac",
        has_fdb=True,
    )
    assert classify(profile, set()) == "router"


# --- o5-A2: full deterministic rule order (new signals) ---------------------
def test_vlan_switch_without_dot1d_fdb_is_switch():
    profile = DeviceProfile(ip="10.0.0.20", responded=True, has_fdb=True, bridge_address=None)
    assert classify(profile, set()) == "switch"


def test_sys_services_l2_with_many_ethernet_ports_is_switch():
    profile = DeviceProfile(
        ip="10.0.0.21",
        responded=True,
        sys_services=oids.SYS_SERVICES_L2,
        interfaces=_eth_ifaces(24),
    )
    assert classify(profile, set()) == "switch"


def test_forwarding_host_without_l3_service_bit_is_not_router():
    profile = DeviceProfile(
        ip="10.0.0.22",
        responded=True,
        ip_forwarding=True,
        sys_services=72,
        interfaces=_eth_ifaces(1),
    )
    assert classify(profile, set()) == "endpoint"


def test_soho_router_with_bridge_is_router():
    # Router rule stands BEFORE the bridge rule -- a SOHO router that also bridges
    # its LAN ports must still resolve to "router", not "switch".
    profile = DeviceProfile(
        ip="10.0.0.23",
        responded=True,
        ip_forwarding=True,
        sys_services=0x4C,
        interfaces=_eth_ifaces(4),
        bridge_address="aa",
    )
    assert classify(profile, set()) == "router"


def test_printer_flag_classifies_printer():
    profile = DeviceProfile(ip="10.0.0.6", responded=True, is_printer=True)
    assert classify(profile, set()) == "printer"


def test_printer_precedence_over_bridge_signal():
    # F3: a network MFU with a Wi-Fi<->Ethernet bridge answers BRIDGE-MIB (has_fdb
    # or bridge_address) too -- the printer rule must win over switch/ap so the
    # verdict doesn't stick as "switch"/"ap" (those never reclassify afterwards).
    profile = DeviceProfile(ip="10.0.0.24", responded=True, is_printer=True, bridge_address="aa")
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
