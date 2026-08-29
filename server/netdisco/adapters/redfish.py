"""Ф9c: Redfish BMC adapter (server serial/model + out-of-band management MAC/IP).

A server's baseboard management controller (iDRAC / iLO / XCC / generic Redfish)
exposes a read-only JSON tree at ``https://<bmc>/redfish/v1/...`` behind a session
token. This adapter:

* ``POST /redfish/v1/SessionService/Sessions`` once to obtain an ``X-Auth-Token``;
* ``GET /redfish/v1/Systems`` -> the system member(s) -> Model / SerialNumber /
  Manufacturer / HostName (the authoritative server identity);
* ``GET /redfish/v1/Managers/<id>/EthernetInterfaces/<id>`` -> the BMC's
  out-of-band NIC MAC + management IP.

It then emits one ``dev_type=server`` identity hint per OOB MAC, carrying the
system's model/serial. When a single BMC fronts several systems (blade chassis),
attributing one blade's serial to the shared OOB NIC would be a false identity, so
model/serial are left empty in that case (UNKNOWN over a guess).

It mirrors the MikroTik/UniFi safety stance: read-only (only the unavoidable login
POST), endpoint-gated to RFC1918, no-redirect opener (SSRF-safe), TLS verify on by
default (explicit per-adapter opt-out is logged), every hop bounded (per-collection
cap + global request budget + body cap), and ``collect()`` NEVER raises -- any
transport/auth/parse failure is recorded in ``errors`` and the cycle moves on. A
hostile ``@odata.id`` is rejected before it is ever fetched (no absolute URL, no
``..`` path escape). The credential lives DPAPI-encrypted in the store (a
``{"username","password"}`` JSON blob), never in config or logs; the HTTP transport
is injectable for tests.
"""

from __future__ import annotations

import json
import logging
import ssl
import urllib.request
from typing import Any, Callable, List, Optional

from server.netdisco.adapters.base import (
    AdapterConfig,
    AdapterNode,
    AdapterResult,
    NetworkAdapter,
)
from server.netdisco.credentials import CredentialStore, default_store
from server.printers.discovery import is_rfc1918

_log = logging.getLogger("srp.netdisco")

_SESSION_PATH = "/redfish/v1/SessionService/Sessions"
_SYSTEMS_PATH = "/redfish/v1/Systems"
_MANAGERS_PATH = "/redfish/v1/Managers"
_TIMEOUT = 8.0  # per request; the login + bounded walk stays under the 30s budget
_MAX_BODY = 4 * 1024 * 1024  # cap a hostile/huge body rather than read it whole
_MAX_MEMBERS = 32  # cap members read from any one collection
_MAX_REQUESTS = 48  # global GET budget per collect() -- the nested tree can't fan out unbounded
_MAX_NAME = 253
_MAX_TEXT = 64  # model / serial / vendor
_MAX_TOKEN = 4096

# transport(path) -> parsed JSON for a GET; raises on transport error.
Transport = Callable[[str], Any]


def _clean_host(name: Any) -> Optional[str]:
    """A BMC-reported host-name trimmed to a safe hostname, or ``None``
    (fail-closed) -- the same allow-list the passive/MikroTik/UniFi paths use."""
    if not isinstance(name, str):
        return None
    host = name.strip().rstrip(".")
    if not host or len(host) > _MAX_NAME or "*" in host:
        return None
    if not all(c.isalnum() or c in "-._" for c in host):
        return None
    return host


def _clean_text(value: Any, maxlen: int = _MAX_TEXT) -> Optional[str]:
    """A bounded printable string (model/serial/vendor), or ``None`` -- rejects
    control characters so a hostile field can't smuggle newlines/NULs into net_*."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > maxlen:
        return None
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        return None
    return text


def _safe_odata(value: Any) -> Optional[str]:
    """A controller-supplied ``@odata.id`` that is safe to fetch, or ``None``.

    The path is appended to the RFC1918 base URL, so it must stay a relative
    Redfish path: it must start with ``/redfish/``, carry only path-safe
    characters, and contain no ``..`` segment. This blocks an absolute URL
    (``http://evil``) or a path-escape from bouncing the request off-host (SSRF)."""
    if not isinstance(value, str):
        return None
    path = value.strip()
    if not path.startswith("/redfish/") or len(path) > 256 or ".." in path:
        return None
    if not all(c.isalnum() or c in "/_-.~" for c in path):
        return None
    return path


def _members(payload: Any, cap: int = _MAX_MEMBERS) -> List[str]:
    """The safe ``@odata.id`` paths of a Redfish collection's members (capped)."""
    if not isinstance(payload, dict):
        return []
    members = payload.get("Members")
    if not isinstance(members, list):
        return []
    out: List[str] = []
    for entry in members:
        if len(out) >= cap:
            break
        if isinstance(entry, dict):
            oid = _safe_odata(entry.get("@odata.id"))
            if oid:
                out.append(oid)
    return out


def _ref(payload: Any, key: str) -> Optional[str]:
    """The safe ``@odata.id`` of a referenced sub-resource (e.g. EthernetInterfaces)."""
    if isinstance(payload, dict):
        ref = payload.get(key)
        if isinstance(ref, dict):
            return _safe_odata(ref.get("@odata.id"))
    return None


def _first_rfc1918_ipv4(value: Any) -> Optional[str]:
    """The first RFC1918 ``IPv4Addresses[].Address`` (management IP), or ``None`` --
    a public/garbage management address never enters net_*."""
    if not isinstance(value, list):
        return None
    for entry in value:
        if isinstance(entry, dict):
            addr = entry.get("Address")
            if isinstance(addr, str) and is_rfc1918(addr):
                return addr
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the endpoint is RFC1918-validated once, so a 3xx
    Location could otherwise bounce the request to an arbitrary host (SSRF)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RedfishAdapter(NetworkAdapter):
    """Read-only Redfish identity adapter. Inject ``transport`` in tests; in
    production it is built from the DPAPI-stored credential (session token) on use."""

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
                return AdapterResult(errors=("redfish: no usable session",))
            errors: List[str] = []
            budget = [_MAX_REQUESTS]
            ident = self._read_identity(transport, budget, errors)
            nodes = self._read_oob_macs(transport, ident, budget, errors)
            return AdapterResult(nodes=tuple(nodes), errors=tuple(errors))
        except Exception:  # collect() must NEVER raise -- one bad adapter can't break the cycle
            _log.exception("redfish adapter failed for %s", self.config.endpoint)
            return AdapterResult(errors=("redfish: unexpected error",))

    # --- parsing -------------------------------------------------------------

    def _get(
        self, transport: Transport, path: str, budget: List[int], errors: List[str], stage: str
    ) -> Any:
        """One budgeted GET. Returns parsed JSON or ``None`` (budget exhausted or a
        per-endpoint failure, recorded once under a fixed ``stage`` label -- never
        the attacker-influenced path)."""
        if budget[0] <= 0:
            return None
        budget[0] -= 1
        try:
            return transport(path)
        except Exception as exc:  # transport/auth/parse error on THIS endpoint only
            errors.append(f"redfish {stage}: {type(exc).__name__}")
            return None

    def _read_identity(
        self, transport: Transport, budget: List[int], errors: List[str]
    ) -> dict[str, Optional[str]]:
        """The single system's identity, or an empty dict when there is not exactly
        one system (none -> nothing to attribute; many -> a shared BMC must not
        inherit one blade's serial)."""
        coll = self._get(transport, _SYSTEMS_PATH, budget, errors, "systems")
        member_paths = _members(coll)
        systems: List[dict[str, Optional[str]]] = []
        for path in member_paths:
            payload = self._get(transport, path, budget, errors, "system")
            if isinstance(payload, dict):
                systems.append(
                    {
                        "model": _clean_text(payload.get("Model")),
                        "serial": _clean_text(payload.get("SerialNumber")),
                        "vendor": _clean_text(payload.get("Manufacturer")),
                        "hostname": _clean_host(payload.get("HostName")),
                    }
                )
        return systems[0] if len(systems) == 1 else {}

    def _read_oob_macs(
        self,
        transport: Transport,
        ident: dict[str, Optional[str]],
        budget: List[int],
        errors: List[str],
    ) -> List[AdapterNode]:
        """Walk Managers -> EthernetInterfaces -> NIC and emit one server identity
        hint per OOB MAC, carrying the (single-)system model/serial."""
        nodes: List[AdapterNode] = []
        coll = self._get(transport, _MANAGERS_PATH, budget, errors, "managers")
        for mgr_path in _members(coll):
            mgr = self._get(transport, mgr_path, budget, errors, "manager")
            ei_path = _ref(mgr, "EthernetInterfaces")
            if not ei_path:
                continue
            ei_coll = self._get(transport, ei_path, budget, errors, "interfaces")
            for nic_path in _members(ei_coll):
                nic = self._get(transport, nic_path, budget, errors, "interface")
                node = _oob_node(nic, ident)
                if node is not None:
                    nodes.append(node)
        return nodes

    # --- real transport (skipped in tests) -----------------------------------

    def _build_transport(self) -> Optional[Transport]:
        """A token-authenticated, no-redirect, RFC1918-gated GET transport, or
        ``None`` if the credential is missing/malformed, the endpoint is not
        private, or the login is rejected."""
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
            username = creds["username"]
            password = creds["password"]
        except (ValueError, KeyError, TypeError):
            return None
        if not isinstance(username, str) or not isinstance(password, str):
            return None
        opener = self._opener()
        base_url = f"https://{self.config.endpoint}"
        token = self._login(opener, base_url, username, password)
        if not token:
            return None

        def transport(path: str) -> Any:
            req = urllib.request.Request(
                base_url + path,
                headers={"X-Auth-Token": token, "Accept": "application/json"},
            )
            # B310: no-redirect opener, hardcoded https:// to an RFC1918 endpoint.
            with opener.open(req, timeout=_TIMEOUT) as resp:  # nosec B310
                body = resp.read(_MAX_BODY)
            return json.loads(body.decode("utf-8", "replace"))

        return transport

    def _opener(self) -> urllib.request.OpenerDirector:
        ctx = ssl.create_default_context()
        if not self.config.tls_verify:
            # On-prem BMCs ship self-signed certs; the operator opts out of
            # verification explicitly per adapter (endpoint is already RFC1918). Leave
            # an audit trail (endpoint only, never the secret).
            _log.warning(
                "redfish adapter %s: TLS verification disabled by config", self.config.endpoint
            )
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ctx))

    def _login(
        self, opener: urllib.request.OpenerDirector, base_url: str, user: str, password: str
    ) -> Optional[str]:
        """The single unavoidable POST: open a session and capture ``X-Auth-Token``.
        Returns ``None`` on any failure (the secret is never logged)."""
        try:
            body = json.dumps({"UserName": user, "Password": password}).encode("utf-8")
            req = urllib.request.Request(
                base_url + _SESSION_PATH,
                data=body,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            # B310: no-redirect opener, hardcoded https:// to an RFC1918 endpoint.
            with opener.open(req, timeout=_TIMEOUT) as resp:  # nosec B310
                token = resp.headers.get("X-Auth-Token")
                resp.read(_MAX_BODY)  # drain
            return _clean_token(token)
        except Exception:
            _log.warning("redfish adapter %s: login failed", self.config.endpoint)
            return None


def _clean_token(value: Any) -> Optional[str]:
    """A session token safe to put in a request header, or ``None`` -- rejects
    control characters (header injection) and an over-long blob."""
    if not isinstance(value, str):
        return None
    token = value.strip()
    if not token or len(token) > _MAX_TOKEN:
        return None
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in token):
        return None
    return token


def _oob_node(nic: Any, ident: dict[str, Optional[str]]) -> Optional[AdapterNode]:
    """One server identity hint from an OOB EthernetInterface, or ``None`` when it
    has no MAC (nothing to dedup/merge on)."""
    if not isinstance(nic, dict):
        return None
    mac = nic.get("MACAddress")
    if not isinstance(mac, str) or not mac:
        return None
    return AdapterNode(
        mac=mac,
        ip=_first_rfc1918_ipv4(nic.get("IPv4Addresses")),
        hostname=ident.get("hostname"),
        vendor=ident.get("vendor"),
        dev_type="server",
        model=ident.get("model"),
        serial=ident.get("serial"),
    )
