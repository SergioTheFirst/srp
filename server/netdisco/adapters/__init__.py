"""Ф9 Tier-3: optional active-equipment adapters (operator-credentialed).

Each adapter pulls ready topology/identity from a controller the operator owns
(MikroTik RouterOS, UniFi, Redfish, NetFlow). All are read-only, isolated, and
fail-soft (``collect()`` never raises), and merge into the existing ``net_*``
backbone by normalised MAC -- they ENRICH, never override validated SNMP.
"""
