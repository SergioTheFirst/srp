"""Ф9a: fold an AdapterResult into the ``net_*`` backbone by normalised MAC.

The merge has exactly two outcomes per adapter node, both safe:

* the MAC is already a known ``net_device`` -> ENRICH its empty identity fields
  (via the Ф8 fill-empty writer, so a validated SNMP/agent value is never
  overridden); or
* the MAC is new -> upsert a fresh ``discovered`` node.

A MAC-less node is skipped (UNKNOWN over a guess -- nothing to dedup on). Link
merge is deferred to a later increment; ``AdapterResult.links``/``identity_map``
are carried but not yet persisted.
"""

from __future__ import annotations

from typing import Any, Callable, List, Optional

from server import db
from server.analytics.oui import normalize_mac, vendor_for_mac
from server.netdisco.adapters.base import AdapterResult
from server.netdisco.identity import device_nid
from server.printers.discovery import is_rfc1918

FillFn = Callable[..., None]
UpsertFn = Callable[..., None]


def merge_adapter_result(
    result: AdapterResult,
    known_devices: List[dict[str, Any]],
    *,
    fill: FillFn = db.fill_net_device_identity,
    upsert: UpsertFn = db.upsert_net_device,
    now: Optional[str] = None,
) -> dict[str, int]:
    """Merge ``result.nodes`` into ``net_*`` deduped by normalised MAC.

    Returns ``{"enriched": E, "added": A}``. ``fill``/``upsert`` are injectable so
    tests run without the DB. Adapter data only ever enriches an empty field or
    adds a wholly new node -- it never overrides a validated identity."""
    by_mac: dict[str, str] = {}
    for dev in known_devices:
        mac = dev.get("mac")
        nid = dev.get("device_nid")
        nm = normalize_mac(mac) if mac else None
        if nm and nid:
            by_mac.setdefault(nm, nid)
    enriched = 0
    added = 0
    for node in result.nodes:
        nm = normalize_mac(node.mac) if node.mac else None
        if not nm:
            continue  # no MAC -> cannot identify or dedup; skip (UNKNOWN over guess)
        # Defense-in-depth at the shared chokepoint: only an RFC1918 address may enter
        # net_* (a controller's ARP table can list a WAN/public peer). Per-adapter
        # gates exist too, but every future adapter (unifi/redfish/flow) reuses THIS
        # merge, so the privacy invariant is enforced here as well.
        ip = node.ip if (node.ip and is_rfc1918(node.ip)) else None
        existing = by_mac.get(nm)
        if existing is not None:
            if node.hostname or node.subtype or node.model:
                fill(existing, hostname=node.hostname, subtype=node.subtype, model=node.model)
                enriched += 1
            continue
        nid = device_nid(mac=node.mac, ip=ip)
        if nid == "nd-unknown":
            continue
        upsert(
            {
                "device_nid": nid,
                "ip": ip,
                "mac": node.mac,
                "vendor": node.vendor or vendor_for_mac(node.mac),
                "hostname": node.hostname,
                "dev_type": node.dev_type or "endpoint",  # it has a MAC -> at least an endpoint
                "subtype": node.subtype,
                "model": node.model,
                "serial": node.serial,
                "status": "discovered",
            },
            now,
        )
        by_mac[nm] = nid  # fold any later node with the same MAC into this one
        added += 1
    return {"enriched": enriched, "added": added}


LinkFn = Callable[..., None]

_MAX_ADAPTER_LINKS = 4096


def merge_adapter_links(
    result: AdapterResult,
    known_devices: List[dict[str, Any]],
    *,
    adapter_type: str = "",
    add_link: LinkFn = db.add_adapter_link,
    now: Optional[str] = None,
) -> int:
    """Persist ``result.links`` into ``net_links``, resolved + deduped by MAC (Ф9d).

    Each endpoint MAC is normalised: a malformed value drops the link (never a
    fabricated edge from a junk ``chassis_id``); a known MAC resolves to its
    canonical nid, an unknown well-formed MAC to a ``nd-mac-`` stub (the assembler
    renders it as a neighbour). Links go in via the ADDITIVE ``add_link`` under
    ``link_kind="adapter"`` and ``confidence="low"`` -- a Tier-3 hint sits below
    validated SNMP (C8) and can never downgrade an SNMP edge. Returns the number
    persisted; ``add_link`` is injectable for tests."""
    by_mac: dict[str, str] = {}
    for dev in known_devices:
        mac = dev.get("mac")
        nid = dev.get("device_nid")
        nm = normalize_mac(mac) if mac else None
        if nm and nid:
            by_mac.setdefault(nm, nid)
    via = f"adapter:{adapter_type}" if adapter_type else "adapter"
    seen: set[tuple[str, str]] = set()
    persisted = 0
    for link in result.links:
        if len(seen) >= _MAX_ADAPTER_LINKS:
            break
        a_nid = _resolve(link.a_mac, by_mac)
        b_nid = _resolve(link.b_mac, by_mac)
        if a_nid is None or b_nid is None or a_nid == b_nid:
            continue  # malformed MAC, or a self-loop -> never a fake/degenerate edge
        pair = (a_nid, b_nid) if a_nid <= b_nid else (b_nid, a_nid)
        if pair in seen:
            continue  # one canonical edge per pair per batch
        seen.add(pair)
        add_link(
            {
                "a_nid": a_nid,
                "b_nid": b_nid,
                "link_kind": "adapter",
                "via_source": via,
                "confidence": "low",
                "medium": None,  # read-side _medium_for_link decides
                "a_port": _clean_port(link.a_port),
                "b_port": _clean_port(link.b_port),
            },
            now,
        )
        persisted += 1
    return persisted


def _clean_port(value: Any) -> Optional[str]:
    """A bounded, control-char-free port label, or ``None`` -- defense-in-depth at the
    shared chokepoint so a future adapter that forgets to sanitise can't land raw text
    in ``net_links`` (today's adapters already clean ports)."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 64:
        return None
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        return None
    return text


def _resolve(mac: Optional[str], by_mac: dict[str, str]) -> Optional[str]:
    """The canonical nid for a link endpoint MAC, or ``None`` when the MAC is
    malformed. A known MAC keeps its canonical (agent/printer/SNMP) nid; an unknown
    but well-formed MAC becomes a ``nd-mac-`` stub the map renders as a neighbour."""
    nm = normalize_mac(mac) if mac else None
    if not nm:
        return None
    known = by_mac.get(nm)
    if known:
        return known
    nid = device_nid(mac=mac)
    return nid if nid != "nd-unknown" else None
