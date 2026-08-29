"""SRP Network Discovery subsystem (server-side).

Generalises the proven printer-discovery engine (stdlib SNMP, bounded active
scan, anti-DoS poll cycle, candidate dedup) into full network-device discovery
plus a persistent L2/L3 topology graph. OFF by default; every probe is
RFC1918-only, read-only and rate-limited. See
docs/superpowers/specs/2026-06-20-network-discovery-rfc.md.
"""
