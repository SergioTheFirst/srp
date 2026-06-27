"""Standard SNMP OIDs for netdisco probe + classification.

Numeric only, never localized text -- the SRP language-independence invariant
(`[[language-independence]]`): a router stays a router on a Russian or Japanese
firmware. Scalars carry the ``.0`` instance suffix and are GET-ready; table bases
are without index, for ``snmp_walk``.

MIBs: MIB-II / SNMPv2-MIB (RFC 1213 / 3418), IP-MIB (RFC 4293), BRIDGE-MIB
(RFC 4188), IF-MIB (RFC 2863), ENTITY-MIB (RFC 4133), Printer-MIB (RFC 3805,
reused via ``printers.classify.is_printer``).
"""

# --- system group (1.3.6.1.2.1.1), GET-ready scalars ------------------------
SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"
SYS_SERVICES = "1.3.6.1.2.1.1.7.0"  # numeric layer bitmask (L2=2, L3=4, ...)

# --- routing: ipForwarding (1=forwarding=router, 2=not-forwarding) ----------
IP_FORWARDING = "1.3.6.1.2.1.4.1.0"

# --- bridging: BRIDGE-MIB ---------------------------------------------------
DOT1D_BASE_BRIDGE_ADDRESS = "1.3.6.1.2.1.17.1.1.0"  # present -> switch candidate
DOT1D_TP_FDB_PORT = "1.3.6.1.2.1.17.4.3.1.2"  # FDB table base; non-empty -> switch

# --- ifTable columns (1.3.6.1.2.1.2.2.1), walked per column -----------------
IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
IF_TYPE = "1.3.6.1.2.1.2.2.1.3"  # numeric ifType (6=ethernet, 71=ieee80211, ...)
IF_SPEED = "1.3.6.1.2.1.2.2.1.5"  # bits/sec
IF_PHYS_ADDRESS = "1.3.6.1.2.1.2.2.1.6"  # MAC (6 raw octets)
IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"  # 1=up, 2=down

# --- ENTITY-MIB physical serial (table base) --------------------------------
ENT_PHYSICAL_SERIAL = "1.3.6.1.2.1.47.1.1.1.1.11"

# --- passive harvest (P7): read neighbour + route tables off infra devices --
# ipNetToMediaPhysAddress (ARP): walked OID suffix = ifIndex.a.b.c.d (the IP),
# value = MAC octets. One walk yields both IP and MAC, no ping.
IP_NET_TO_MEDIA_PHYS = "1.3.6.1.2.1.4.22.1.2"
# ipCidrRouteIfIndex: the ipCidrRouteTable INDEX is dest(4).mask(4).tos(1).
# nextHop(4), so walking just the ifIndex column recovers dest/mask/next-hop from
# the OID suffix (+ ifIndex from the value) in a single walk.
IP_CIDR_ROUTE_IF_INDEX = "1.3.6.1.2.1.4.24.4.1.5"

# --- P8 topology evidence: LLDP / CDP / bridge-FDB / STP --------------------
# LLDP-MIB (IEEE 802.1AB), lldpRemTable columns. Index = lldpRemTimeMark.
# lldpRemLocalPortNum.lldpRemIndex, so the local port number rides in the OID
# suffix. Authoritative neighbour source (standards-based).
LLDP_REM_CHASSIS_ID = "1.0.8802.1.1.2.1.4.1.1.5"  # remote chassis id (often a MAC)
LLDP_REM_PORT_ID = "1.0.8802.1.1.2.1.4.1.1.7"  # remote port id
# lldpRemPortDesc = the textual description of the *remote* port; carried so a
# directed port<->port link can show both ends' labels (Ф7 T1).
LLDP_REM_PORT_DESC = "1.0.8802.1.1.2.1.4.1.1.8"
# lldpRemManAddrTable (Ф7 T2): the remote management address. The index is
# TimeMark.LocalPortNum.RemIndex.AddrSubtype.AddrLen...; the value is the address
# bytes. A neighbour's mgmt IP extends the seed set without a ping.
LLDP_REM_MAN_ADDR = "1.0.8802.1.1.2.1.4.2.1.5"
# lldpLocPortDesc = the *local* port's textual description; keyed by
# lldpLocPortNum so an ifIndex->name map can be derived where ifXTable is absent.
LLDP_LOC_PORT_DESC = "1.0.8802.1.1.2.1.3.7.1.4"
# CISCO-CDP-MIB cdpCacheTable columns. Index = cdpCacheIfIndex.cdpCacheDeviceIndex,
# so the local ifIndex rides in the OID suffix. Cisco-only, high authority.
CDP_CACHE_DEVICE_ID = "1.3.6.1.4.1.9.9.23.1.2.1.1.6"  # remote device id (text)
CDP_CACHE_DEVICE_PORT = "1.3.6.1.4.1.9.9.23.1.2.1.1.7"  # remote port (text)
# BRIDGE-MIB: bridge port -> ifIndex (resolves the FDB port number to an ifTable
# interface). dot1dTpFdbPort (MAC -> bridge port) is defined above.
DOT1D_BASE_PORT_IF_INDEX = "1.3.6.1.2.1.17.1.4.1.2"
# STP: designated bridge per port -- disambiguates uplink direction ("who is
# higher") during fusion (P9). Defined here with the rest of the topology OIDs.
DOT1D_STP_PORT_DESIGNATED_BRIDGE = "1.3.6.1.2.1.17.2.15.1.8"
# STP role/state (Ф7 T7): dot1dStpPortRole (1=disabled,2=root,3=designated,
# 4=alternate,5=forwarding-pending in RSTP) and dot1dStpPortState (1..6). A
# "designated"/"root" port points the uplink direction for non-LLDP switches.
DOT1D_STP_PORT_ROLE = "1.3.6.1.2.1.17.2.15.1.16"
DOT1D_STP_PORT_STATE = "1.3.6.1.2.1.17.2.15.1.3"

# --- Ф7 T4: Q-BRIDGE-MIB VLAN forwarding DB (dot1q, fixes dot1d on VLAN switches)
# dot1qTpFdbPort: index = dot1qFdbId(=vlan).mac6; value = the bridge port. The VLAN
# rides in the OID prefix so each learned MAC carries its VLAN -- a VLAN-aware
# switch that returns nothing from dot1d is recovered here. dot1qPvid maps a port
# to its default VLAN (untagged egress).
DOT1Q_TP_FDB_PORT = "1.3.6.1.2.1.17.7.1.2.2.1.2"
DOT1Q_PVID = "1.3.6.1.2.1.17.7.1.4.5.1.1"

# --- Ф7 T5: IF-MIB ifXTable (port labels / uplink aliases) -------------------
# ifName/ifAlias (RFC 2863): the human port label ("Gi1/0/24") and the operator
# description ("uplink to core"). ifName resolves an ifIndex to a real port the
# way ifDescr never quite does; carried into net_links ports + net_interfaces.
IF_NAME = "1.3.6.1.2.1.31.1.1.1.1"
IF_ALIAS = "1.3.6.1.2.1.31.1.1.1.18"

# --- Ф7 T3: LLDP-MED device class + Ф7 T7 supporting MIBs --------------------
# LLDP-EXT-MED lldpXmedRemDeviceClass (1=not-defined..10=telephone,6=access-point,
# 8=switch,...). Identifies a phone/AP neighbour by its LLDP advertisement, not a
# guess from sysObjectID (UNKNOWN over a vendor-oid inference).
LLDP_XMED_REM_DEVICE_CLASS = "1.0.8802.1.1.2.1.5.1.1.11"
# ENTITY-MIB entPhysicalModelName: the chassis model string -> exact icon/type
# (a "Catalyst 2960" model confirms a switch beyond the bridge-address heuristic).
ENT_PHYSICAL_MODEL_NAME = "1.3.6.1.2.1.47.1.1.1.1.13"
# POWER-ETHERNET-MIB pethPsePortDetectionStatus: 1=disabled,2=searching,..,4=delivering
# power. A port *delivering* power corroborates a powered AP/phone behind it.
PETH_PSE_PORT_DETECTION_STATUS = "1.3.6.1.2.1.105.1.1.1.6"
# HOST-RESOURCES-MIB hrSWRunName: running processes. A server exposes services
# (an OS/hypervisor) that an endpoint does not; bounded to running rows only.
HR_SW_RUN_NAME = "1.3.6.1.2.1.25.4.2.1.2"
# IP-MIB ipNetToPhysicalPhysAddress: the IPv6 neighbour cache (ND) -> MAC, the
# IPv6 analogue of ipNetToMediaPhysAddress (which is IPv4-only ARP).
IP_NET_TO_PHYSICAL_PHYS = "1.3.6.1.2.1.4.35.1.4"

# --- Printer-MIB root (presence => printer, via printers.classify.is_printer)
PRINTER_MIB = "1.3.6.1.2.1.43"

# sysServices layer bits (RFC 1213): datalink (L2) and internet/routing (L3).
SYS_SERVICES_L2 = 0x02
SYS_SERVICES_L3 = 0x04

# ifType for a wireless radio (IANA ifType 71 = ieee80211) -> AP signal.
IF_TYPE_IEEE80211 = 71

# --- Ф7 T6: wireless controller vendor roots (sysObjectID prefixes) ----------
# Only these enterprise roots are walked for the client->AP association tables --
# a generic host that happens to answer SNMP is never mistaken for a WLC. Walked
# ONLY when the probed device's sysObjectID starts with one of these (fail-closed).
# AIRESPACE-WIRELESS-MIB (Cisco WLC, acquired AIRESPACE enterprise 14179).
WLC_ROOT_AIRESPACE = "1.3.6.1.4.1.14179"
# AIRESPACE bsnMobileStationTable: the client association table. Index encodes
# the client MAC; the AP MAC rides in a sibling column (bsnMobileStationApMacAddr).
BSN_MOBILE_STATION_MAC = "1.3.6.1.4.1.14179.2.1.4.1.1.1"  # client MAC (index)
BSN_MOBILE_STATION_AP_MAC = "1.3.6.1.4.1.14179.2.1.4.1.1.26"  # serving AP MAC
# Aruba (enterprise 14823): wlsxUserTable / nUserTable columns.
WLC_ROOT_ARUBA = "1.3.6.1.4.1.14823"
ARUBA_USER_STA_MAC = "1.3.6.1.4.1.14823.2.2.1.5.1.1.1"  # client MAC
ARUBA_USER_AP_MAC = "1.3.6.1.4.1.14823.2.2.1.5.1.1.3"  # serving AP MAC
# MikroTik RouterOS (enterprise 14988): mtxrWlRtab* registration table.
WLC_ROOT_MIKROTIK = "1.3.6.1.4.1.14988"
MTXR_WL_REG_CLIENT_MAC = "1.3.6.1.4.1.14988.1.1.1.5.1.1"  # client MAC (index part)
MTXR_WL_REG_AP_MAC = "1.3.6.1.4.1.14988.1.1.1.5.1.3"  # serving AP MAC

# --- Ф7 T3: LLDP-MED device-class numeric values (lldpXmedRemDeviceClass) ----
# Mapping the advertisement's numeric class to a stable subtype label. Only the
# ones that change a node's type/icon; anything else stays UNKNOWN (not a guess).
LLDP_MED_PHONE = 10  # "Network Connectivity -> Voice" endpoint
LLDP_MED_AP = 6  # "Access Point" (wireless infrastructure)
LLDP_MED_SERVER = 5  # "Server" (rarely advertised, but authoritative when present)
