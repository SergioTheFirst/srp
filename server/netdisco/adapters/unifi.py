"""Ф9b: UniFi Network controller adapter (identity from devices + clients).

A self-hosted UniFi Network controller exposes a read-only JSON API at
``https://<host>:8443/api/s/<site>/...`` behind a cookie session: one ``POST
/api/login`` seeds the session cookie, then ``GET .../stat/device`` (managed APs/
switches/gateways) and ``GET .../stat/sta`` (connected clients) return a
``{"meta": ..., "data": [...]}`` envelope. This adapter folds those rows into
identity hints per MAC and emits LLDP/uplink link hints (carried for a later
link-merge increment -- the merge persists nodes only for now).

It mirrors the MikroTik adapter's safety stance: read-only (only the unavoidable
login POST), endpoint-gated to RFC1918, no-redirect opener (SSRF-safe), TLS verify
on by default (explicit per-adapter opt-out is logged), hard-timeout- and
size-bounded, and ``collect()`` NEVER raises -- any transport/auth/parse failure is
recorded in ``errors`` and the cycle moves on. The credential lives DPAPI-encrypted
in the store (a ``{"username","password"}`` JSON blob), never in config or logs; the
HTTP transport is injectable for tests.

UniFi OS gateways (UDM/Cloud Key gen2: port 443, ``/proxy/network`` prefix, JWT +
``X-CSRF-Token``) are a documented carry-forward; this increment targets the classic
self-hosted controller on 8443.
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import ssl
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

from server.netdisco.adapters.base import (
    AdapterConfig,
    AdapterLink,
    AdapterNode,
    AdapterResult,
    NetworkAdapter,
)
from server.netdisco.credentials import CredentialStore, default_store
from server.printers.discovery import is_rfc1918

_log = logging.getLogger("srp.netdisco")

_CONTROLLER_PORT = 8443  # classic self-hosted UniFi Network controller
_LOGIN_PATH = "/api/login"
_DEVICE_SUFFIX = "/stat/device"
_CLIENT_SUFFIX = "/stat/sta"
_TIMEOUT = 8.0  # per request; login + 2 GETs stays under the 30s adapter budget
_MAX_BODY = 8 * 1024 * 1024  # cap a hostile/huge table rather than read it whole
_MAX_ROWS = 4096  # cap nodes folded per endpoint (a flood can't balloon net_*)
_MAX_LINKS = 8192  # overall link cap (lldp_table is per-device, but bound the total)
_MAX_NAME = 253
_MAX_TEXT = 64  # model / serial / port labels

# transport(path) -> parsed JSON (the controller's envelope); raises on transport error.
Transport = Callable[[str], Any]

# UniFi device ``type`` -> (dev_type, subtype). subtype only where it matches the
# LLDP-MED vocabulary (phone/ap/server) so it can enrich an existing SNMP node;
# switch/router have no subtype. An unmapped type leaves dev_type None (the merge
# then treats a MAC-bearing node as a generic endpoint).
_DEV_TYPE: Dict[str, Tuple[str, Optional[str]]] = {
    "uap": ("ap", "ap"),
    "usw": ("switch", None),
    "ugw": ("router", None),
    "udm": ("router", None),
    "uxg": ("router", None),
    "uph": ("phone", "phone"),
}


def _clean_host(name: Any) -> Optional[str]:
    """A controller-reported name trimmed to a safe hostname, or ``None``
    (fail-closed) -- the same allow-list the passive/MikroTik paths use."""
    if not isinstance(name, str):
        return None
    host = name.strip().rstrip(".")
    if not host or len(host) > _MAX_NAME or "*" in host:
        return None
    if not all(c.isalnum() or c in "-._" for c in host):
        return None
    return host


def _clean_text(value: Any, maxlen: int) -> Optional[str]:
    """A bounded printable string (model/serial/port), or ``None`` -- rejects
    control characters so a hostile field can't smuggle newlines/NULs into net_*."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > maxlen:
        return None
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        return None
    return text


def _safe_site(site: str) -> str:
    """A path-safe site id (allow-list), defaulting to ``default`` -- never let a
    configured site smuggle ``../`` or other path tricks into the controller URL."""
    if site and len(site) <= _MAX_TEXT and all(c.isalnum() or c in "-._" for c in site):
        return site
    return "default"


def _port_label(value: Any) -> Optional[str]:
    if isinstance(value, int):
        return str(value)
    return _clean_text(value, _MAX_TEXT)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the endpoint is RFC1918-validated once, so a 3xx
    Location could otherwise bounce the request to an arbitrary host (SSRF)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UniFiAdapter(NetworkAdapter):
    """Read-only UniFi Network identity adapter. Inject ``transport`` in tests; in
    production it is built from the DPAPI-stored credential (cookie login) on use."""

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
                return AdapterResult(errors=("unifi: no usable session",))
            devices: List[dict] = []
            clients: List[dict] = []
            errors: List[str] = []
            self._harvest(transport, self._device_path(), devices, errors)
            self._harvest(transport, self._client_path(), clients, errors)
            nodes, links = self._build(devices, clients)
            return AdapterResult(nodes=nodes, links=links, errors=tuple(errors))
        except Exception:  # collect() must NEVER raise -- one bad adapter can't break the cycle
            _log.exception("unifi adapter failed for %s", self.config.endpoint)
            return AdapterResult(errors=("unifi: unexpected error",))

    # --- paths ---------------------------------------------------------------

    def _site(self) -> str:
        return _safe_site(self.config.site_id)

    def _device_path(self) -> str:
        return f"/api/s/{self._site()}{_DEVICE_SUFFIX}"

    def _client_path(self) -> str:
        return f"/api/s/{self._site()}{_CLIENT_SUFFIX}"

    # --- parsing -------------------------------------------------------------

    def _harvest(
        self, transport: Transport, path: str, sink: List[dict], errors: List[str]
    ) -> None:
        """Fetch one endpoint, unwrap the ``data`` envelope, and append its row
        dicts to ``sink`` (capped). A per-endpoint failure is isolated."""
        try:
            payload = transport(path)
        except Exception as exc:  # transport/auth/parse error on THIS endpoint only
            errors.append(f"unifi {path}: {type(exc).__name__}")
            return
        rows = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return
        for row in rows:
            if len(sink) >= _MAX_ROWS:
                break
            if isinstance(row, dict):
                sink.append(row)

    def _build(
        self, devices: List[dict], clients: List[dict]
    ) -> Tuple[Tuple[AdapterNode, ...], Tuple[AdapterLink, ...]]:
        """Fold device + client rows into one node per MAC (device fields are
        richer, so they seed; clients fill empty), plus LLDP/uplink link hints."""
        acc: Dict[str, Dict[str, Any]] = {}
        links: List[AdapterLink] = []
        for row in devices:
            slot = self._slot(row, acc)
            if slot is None:
                continue
            dev_type, subtype = _DEV_TYPE.get(_dtype(row), (None, None))
            if dev_type and not slot.get("dev_type"):
                slot["dev_type"] = dev_type
            if subtype and not slot.get("subtype"):
                slot["subtype"] = subtype
            self._fill_common(row, slot, ("name",))
            model = _clean_text(row.get("model"), _MAX_TEXT)
            if model and not slot.get("model"):
                slot["model"] = model
            serial = _clean_text(row.get("serial"), _MAX_TEXT)
            if serial and not slot.get("serial"):
                slot["serial"] = serial
            self._collect_links(slot["mac"], row, links)  # _slot proved a non-empty str
        for row in clients:
            slot = self._slot(row, acc)
            if slot is None:
                continue
            if not slot.get("dev_type"):
                slot["dev_type"] = "endpoint"  # a connected client is at least an endpoint
            self._fill_common(row, slot, ("hostname", "name"))
        nodes = tuple(
            AdapterNode(
                mac=s["mac"],
                ip=s.get("ip"),
                hostname=s.get("hostname"),
                dev_type=s.get("dev_type"),
                subtype=s.get("subtype"),
                model=s.get("model"),
                serial=s.get("serial"),
            )
            for s in acc.values()
        )
        return nodes, tuple(links)

    @staticmethod
    def _slot(row: dict, acc: Dict[str, Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        mac = row.get("mac")
        if not isinstance(mac, str) or not mac:
            return None
        return acc.setdefault(mac.lower(), {"mac": mac})

    @staticmethod
    def _fill_common(row: dict, slot: Dict[str, Any], name_keys: Tuple[str, ...]) -> None:
        ip = row.get("ip")
        if isinstance(ip, str) and is_rfc1918(ip) and not slot.get("ip"):
            slot["ip"] = ip  # RFC1918 only -- never import a WAN/public address
        if not slot.get("hostname"):
            for key in name_keys:
                host = _clean_host(row.get(key))
                if host:
                    slot["hostname"] = host
                    break

    @staticmethod
    def _collect_links(mac: str, row: dict, links: List[AdapterLink]) -> None:
        if not mac or len(links) >= _MAX_LINKS:
            return
        uplink = row.get("uplink")
        if isinstance(uplink, dict):
            peer = uplink.get("uplink_mac")
            if isinstance(peer, str) and peer:
                links.append(AdapterLink(a_mac=mac, b_mac=peer, link_kind="uplink"))
        table = row.get("lldp_table")
        if isinstance(table, list):
            for entry in table:
                if len(links) >= _MAX_LINKS:
                    break
                if not isinstance(entry, dict):
                    continue
                peer = entry.get("chassis_id")
                if isinstance(peer, str) and peer:
                    links.append(
                        AdapterLink(
                            a_mac=mac,
                            b_mac=peer,
                            link_kind="lldp",
                            a_port=_port_label(entry.get("local_port_idx")),
                            b_port=_port_label(entry.get("port_id")),
                        )
                    )

    # --- real transport (skipped in tests) -----------------------------------

    def _build_transport(self) -> Optional[Transport]:
        """A cookie-authenticated, no-redirect, RFC1918-gated GET transport, or
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
        base_url = f"https://{self.config.endpoint}:{_CONTROLLER_PORT}"
        if not self._login(opener, base_url, username, password):
            return None

        def transport(path: str) -> Any:
            req = urllib.request.Request(base_url + path, headers={"Accept": "application/json"})
            # B310: no-redirect opener, hardcoded https:// to an RFC1918 endpoint.
            with opener.open(req, timeout=_TIMEOUT) as resp:  # nosec B310
                body = resp.read(_MAX_BODY)
            return json.loads(body.decode("utf-8", "replace"))

        return transport

    def _opener(self) -> urllib.request.OpenerDirector:
        ctx = ssl.create_default_context()
        if not self.config.tls_verify:
            # On-prem controllers ship self-signed certs; the operator opts out of
            # verification explicitly per adapter (endpoint is already RFC1918). Leave
            # an audit trail (endpoint only, never the secret).
            _log.warning(
                "unifi adapter %s: TLS verification disabled by config", self.config.endpoint
            )
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        jar = http.cookiejar.CookieJar()
        return urllib.request.build_opener(
            _NoRedirect,
            urllib.request.HTTPSHandler(context=ctx),
            urllib.request.HTTPCookieProcessor(jar),
        )

    def _login(
        self, opener: urllib.request.OpenerDirector, base_url: str, user: str, password: str
    ) -> bool:
        """The single unavoidable POST: seed the session cookie. Returns False on
        any failure (the secret is never logged)."""
        try:
            body = json.dumps({"username": user, "password": password}).encode("utf-8")
            req = urllib.request.Request(
                base_url + _LOGIN_PATH,
                data=body,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            # B310: no-redirect opener, hardcoded https:// to an RFC1918 endpoint.
            with opener.open(req, timeout=_TIMEOUT) as resp:  # nosec B310
                resp.read(_MAX_BODY)  # drain; the session lives in the cookie jar
            return True
        except Exception:
            _log.warning("unifi adapter %s: login failed", self.config.endpoint)
            return False


def _dtype(row: dict) -> str:
    value = row.get("type")
    return value if isinstance(value, str) else ""
