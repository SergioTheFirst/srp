"""Ф9a: the adapter contract — config, result, and the read-only ABC.

An adapter is a thin, read-only client for one operator-owned controller. The
single hard rule: :meth:`NetworkAdapter.collect` MUST NEVER raise. A transient,
auth, TLS or parse failure is reported in :attr:`AdapterResult.errors` and the
cycle moves on to the next adapter -- one bad controller can never break the
others or the poll loop. Everything an adapter returns is a *hint* that the merge
layer folds into ``net_*`` by normalised MAC, only ever filling empty fields
(never overriding a validated SNMP identity).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Mapping, Optional, Tuple

# Adapter types the loader/cycle know how to build (others are rejected on load).
KNOWN_ADAPTER_TYPES: frozenset[str] = frozenset({"mikrotik", "unifi", "redfish", "flow"})


@dataclass(frozen=True)
class AdapterConfig:
    """One configured adapter. ``credential`` names a DPAPI secret in the store
    (never the secret itself); ``endpoint`` is validated RFC1918 on config load."""

    adapter_type: str
    endpoint: str
    credential: str = ""  # CredentialStore secret ref; "" = no auth
    tls_verify: bool = True
    site_id: str = ""

    def __repr__(self) -> str:
        # Endpoint/type are fine to log; there is no secret here (credential is a
        # ref name, not the secret) but keep the repr minimal and stable.
        return f"AdapterConfig(adapter_type={self.adapter_type!r}, endpoint={self.endpoint!r})"

    __str__ = __repr__


@dataclass(frozen=True)
class AdapterNode:
    """An identity hint for one host as seen by a controller. All fields optional
    but a MAC (or IP) is needed for the merge to place it."""

    mac: Optional[str] = None
    ip: Optional[str] = None
    hostname: Optional[str] = None
    vendor: Optional[str] = None
    dev_type: Optional[str] = None
    subtype: Optional[str] = None
    model: Optional[str] = None
    serial: Optional[str] = None


@dataclass(frozen=True)
class AdapterLink:
    """A link hint between two MAC-identified endpoints (merged in a later phase)."""

    a_mac: str
    b_mac: str
    link_kind: str = "adapter"
    a_port: Optional[str] = None
    b_port: Optional[str] = None


@dataclass(frozen=True)
class AdapterResult:
    """What one ``collect()`` produced. ``identity_map`` (external-node-id -> MAC)
    seats future link resolution; ``errors`` carries human-readable, secret-free
    failure notes so the cycle can record them without raising."""

    nodes: Tuple[AdapterNode, ...] = ()
    links: Tuple[AdapterLink, ...] = ()
    identity_map: Mapping[str, str] = field(default_factory=dict)
    errors: Tuple[str, ...] = ()


class NetworkAdapter(ABC):
    """Read-only, fail-soft adapter base. Subclasses implement :meth:`collect`,
    which must return an :class:`AdapterResult` and never raise."""

    def __init__(self, config: AdapterConfig) -> None:
        self.config = config

    @abstractmethod
    def collect(self) -> AdapterResult:
        """Fetch and parse the controller, returning hints. Never raises."""
        raise NotImplementedError
