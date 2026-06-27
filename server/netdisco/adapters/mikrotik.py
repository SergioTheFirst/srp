"""Ф9a: MikroTik RouterOS REST adapter (identity from ARP + DHCP leases).

RouterOS v7 exposes a read-only REST API at ``https://<router>/rest/...`` behind
HTTP Basic auth. This adapter pulls the ARP table (IP<->MAC) and the DHCP-server
leases (IP<->MAC<->hostname) and merges them per MAC into identity hints. It is
read-only (only GETs), endpoint-gated to RFC1918, hard-timeout-bounded, and never
raises -- any transport/auth/parse failure is recorded in ``errors`` and the cycle
moves on. The credential lives DPAPI-encrypted in the store (a
``{"username","password"}`` JSON blob), never in config or logs; the HTTP transport
is injectable for tests.

Links (``/ip/neighbor``) and L2 ports (``/interface/bridge/host``) are deferred to a
later increment; this one establishes authoritative IP<->MAC<->hostname identity.
"""

from __future__ import annotations

import base64
import json
import logging
import ssl
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from server.netdisco.adapters.base import (
    AdapterConfig,
    AdapterNode,
    AdapterResult,
    NetworkAdapter,
)
from server.netdisco.credentials import CredentialStore, default_store
from server.printers.discovery import is_rfc1918

_log = logging.getLogger("srp.netdisco")

_ARP_PATH = "/ip/arp"
_LEASE_PATH = "/ip/dhcp-server/lease"
_TIMEOUT = 10.0  # per-request; two requests stay well under the 30s adapter budget
_MAX_BODY = 4 * 1024 * 1024  # cap a hostile/huge table rather than read it whole
_MAX_NAME = 253

# transport(path) -> parsed JSON (a list of row dicts); raises on transport error.
Transport = Callable[[str], Any]


def _clean_host(name: Any) -> Optional[str]:
    """A lease host-name trimmed to a safe hostname, or ``None`` (fail-closed) --
    the same allow-list the passive/banner paths use at their trust boundary."""
    if not isinstance(name, str):
        return None
    host = name.strip().rstrip(".")
    if not host or len(host) > _MAX_NAME or "*" in host:
        return None
    if not all(c.isalnum() or c in "-._" for c in host):
        return None
    return host


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the endpoint is RFC1918-validated once, so a 3xx
    Location could otherwise bounce the request to an arbitrary host (SSRF)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MikroTikAdapter(NetworkAdapter):
    """Read-only RouterOS REST identity adapter. Inject ``transport`` in tests;
    in production it is built from the DPAPI-stored credential on first use."""

    def __init__(
        self,
        config: AdapterConfig,
        *,
        transport: Optional[Transport] = None,
        store: Optional[CredentialStore] = None,
    ) -> None:
        super().__init__(config)
        self._transport = transport
        self._store = store

    def collect(self) -> AdapterResult:
        try:
            transport = self._transport or self._build_transport()
            if transport is None:
                return AdapterResult(errors=("mikrotik: no usable credential",))
            acc: Dict[str, dict[str, Any]] = {}
            errors: List[str] = []
            self._harvest(transport, _ARP_PATH, acc, errors)
            self._harvest(transport, _LEASE_PATH, acc, errors)
            nodes = tuple(
                AdapterNode(mac=s["mac"], ip=s.get("ip"), hostname=s.get("hostname"))
                for s in acc.values()
            )
            return AdapterResult(nodes=nodes, errors=tuple(errors))
        except Exception:  # collect() must NEVER raise -- one bad adapter can't break the cycle
            _log.exception("mikrotik adapter failed for %s", self.config.endpoint)
            return AdapterResult(errors=("mikrotik: unexpected error",))

    # --- parsing -------------------------------------------------------------

    def _harvest(
        self, transport: Transport, path: str, acc: Dict[str, dict[str, Any]], errors: List[str]
    ) -> None:
        """Fetch one endpoint and fold its rows into ``acc`` keyed by MAC. A
        per-endpoint failure is isolated (recorded, other endpoints continue)."""
        try:
            rows = transport(path)
        except Exception as exc:  # transport/auth/parse error on THIS endpoint only
            errors.append(f"mikrotik {path}: {type(exc).__name__}")
            return
        if not isinstance(rows, list):
            return
        for row in rows:
            if not isinstance(row, dict):
                continue
            self._add_row(row, acc)

    def _add_row(self, row: dict[str, Any], acc: Dict[str, dict[str, Any]]) -> None:
        mac = row.get("mac-address")
        if not isinstance(mac, str) or not mac:
            return
        slot = acc.setdefault(mac.lower(), {"mac": mac})
        ip = row.get("address")
        if isinstance(ip, str) and is_rfc1918(ip) and not slot.get("ip"):
            slot["ip"] = ip  # RFC1918 only -- never import a WAN/public ARP entry
        host = _clean_host(row.get("host-name"))
        if host and not slot.get("hostname"):
            slot["hostname"] = host

    # --- real transport (skipped in tests) -----------------------------------

    def _build_transport(self) -> Optional[Transport]:
        """An authenticated, no-redirect, RFC1918-gated GET transport, or ``None``
        if the credential is missing/malformed or the endpoint is not private."""
        if not is_rfc1918(self.config.endpoint):
            return None  # defense-in-depth: never reach off-LAN even if config slipped
        store = self._store or default_store()
        secret = (
            store.get_secret(self.config.credential)
            if (store is not None and self.config.credential)
            else None
        )
        if not secret:
            return None
        try:
            creds = json.loads(secret)
            auth_raw = f"{creds['username']}:{creds['password']}".encode("utf-8")
        except (ValueError, KeyError, TypeError):
            return None
        auth = base64.b64encode(auth_raw).decode("ascii")
        base_url = f"https://{self.config.endpoint}/rest"
        ctx = ssl.create_default_context()
        if not self.config.tls_verify:
            # On-prem LAN devices ship self-signed certs; the operator opts out of
            # verification explicitly per adapter. Endpoint is already RFC1918. Leave
            # an audit trail (endpoint only, never the secret) so an accidental opt-out
            # is visible.
            _log.warning(
                "mikrotik adapter %s: TLS verification disabled by config", self.config.endpoint
            )
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ctx))

        def transport(path: str) -> Any:
            req = urllib.request.Request(
                base_url + path,
                headers={"Authorization": f"Basic {auth}", "Accept": "application/json"},
            )
            # B310: no-redirect opener, hardcoded https:// to an RFC1918 endpoint.
            with opener.open(req, timeout=_TIMEOUT) as resp:  # nosec B310
                body = resp.read(_MAX_BODY)
            return json.loads(body.decode("utf-8", "replace"))

        return transport
