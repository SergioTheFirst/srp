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

# --- Printer-MIB root (presence => printer, via printers.classify.is_printer)
PRINTER_MIB = "1.3.6.1.2.1.43"

# sysServices layer bits (RFC 1213): datalink (L2) and internet/routing (L3).
SYS_SERVICES_L2 = 0x02
SYS_SERVICES_L3 = 0x04

# ifType for a wireless radio (IANA ifType 71 = ieee80211) -> AP signal.
IF_TYPE_IEEE80211 = 71
