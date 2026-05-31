"""Transport: deliver envelopes to the server, buffer to disk when offline.

Pure stdlib (urllib) -- the agent ships with zero third-party dependencies, so
it drops onto a domain PC without a pip install. Envelopes that cannot be sent
(server down, network blip, 5xx) are appended to a JSONL buffer and replayed
FIFO on the next successful contact. A payload the server *rejects* (HTTP 4xx)
is dropped, not buffered: retrying a poison message forever would wedge the
queue behind it.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

from client.config import ClientConfig

log = logging.getLogger(__name__)

# Stamped onto every envelope. Keep in sync with shared.schema.CONTRACT_VERSION
# (duplicated, not imported, so the client needs no pydantic install).
AGENT_VERSION = "0.1.0"

_MAX_BUFFER_LINES = 5000  # oldest dropped past this -- bound disk use
_SEND_ATTEMPTS = 2  # quick in-process retries before buffering
_RETRY_BACKOFF_SEC = 1.0


class Transport:
    """Stateless-ish sender bound to one client config."""

    def __init__(self, cfg: ClientConfig) -> None:
        self._cfg = cfg
        self._ingest_url = cfg.server_url.rstrip("/") + "/api/v1/ingest"
        self._buffer = cfg.resolved_buffer_path()

    # -- public API -------------------------------------------------------- #
    def send(
        self,
        msg_type: str,
        payload: Optional[dict[str, Any]],
        source_health: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Build and deliver an envelope. Buffer it on transient failure.

        Returns True if the envelope was delivered (or permanently rejected),
        False if it was buffered for a later retry. An envelope with no payload
        is still sent when it carries source_health (so the server learns a
        source is down); a fully empty send is a no-op.
        """
        if payload is None and not source_health:
            log.debug("skipping %s: no payload and no source health", msg_type)
            return True
        envelope = self._envelope(msg_type, payload or {}, source_health)
        if self._deliver(envelope):
            self.flush_buffer()  # server is reachable -- drain backlog too
            return True
        self._append_buffer(envelope)
        return False

    def flush_buffer(self) -> int:
        """Replay buffered envelopes oldest-first.

        Stops at the first transient failure, keeping that envelope and every
        later one for next time. Returns how many were cleared (sent/dropped).
        """
        lines = self._read_buffer()
        if not lines:
            return 0
        handled = 0
        remaining: list[str] = []
        blocked = False
        for line in lines:
            if blocked:
                remaining.append(line)
                continue
            try:
                envelope = json.loads(line)
            except json.JSONDecodeError:
                log.warning("dropping corrupt buffer line")
                handled += 1
                continue
            if self._deliver(envelope):
                handled += 1
            else:
                blocked = True
                remaining.append(line)
        self._write_buffer(remaining)
        if handled:
            log.info("flushed %d buffered envelope(s), %d remaining", handled, len(remaining))
        return handled

    # -- delivery ---------------------------------------------------------- #
    def _envelope(
        self, msg_type: str, payload: dict[str, Any], source_health: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        return {
            "device_id": self._cfg.device_id,
            "agent_version": AGENT_VERSION,
            "msg_type": msg_type,
            "ts": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
            "source_health": source_health or {},
            # W1.1: send None when empty so the server's COALESCE keeps any
            # existing value rather than overwriting a known site with NULL.
            "site_code": self._cfg.site_code or None,
            "site_name": self._cfg.site_name or None,
        }

    def _deliver(self, envelope: dict[str, Any]) -> bool:
        """True if handled (delivered or 4xx-rejected); False if it should buffer."""
        for attempt in range(1, _SEND_ATTEMPTS + 1):
            outcome = self._attempt(envelope)
            if outcome in ("ok", "drop"):
                return True
            if attempt < _SEND_ATTEMPTS:
                time.sleep(_RETRY_BACKOFF_SEC)
        return False

    def _attempt(self, envelope: dict[str, Any]) -> str:
        """One POST. Returns 'ok' | 'drop' | 'retry'."""
        body = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._cfg.ingest_token:
            headers["X-SRP-Token"] = self._cfg.ingest_token
        req = urllib.request.Request(
            self._ingest_url,
            data=body,
            method="POST",
            headers=headers,
        )
        try:
            # B310: scheme is the operator-configured server_url, not user input.
            with urllib.request.urlopen(req, timeout=self._cfg.http_timeout_sec):  # nosec B310
                return "ok"  # urlopen only returns for 2xx/3xx
        except urllib.error.HTTPError as exc:  # subclass of URLError -> catch first
            if 400 <= exc.code < 500:
                log.warning(
                    "server rejected %s (HTTP %d) -- dropping", envelope.get("msg_type"), exc.code
                )
                return "drop"
            log.warning("server error HTTP %d on %s", exc.code, envelope.get("msg_type"))
            return "retry"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.warning("network error sending %s: %s", envelope.get("msg_type"), exc)
            return "retry"

    # -- buffer I/O -------------------------------------------------------- #
    def _read_buffer(self) -> list[str]:
        if not self._buffer.exists():
            return []
        try:
            lines = self._buffer.read_text(encoding="utf-8").splitlines()
            return [ln for ln in lines if ln.strip()]
        except OSError as exc:
            log.error("could not read buffer: %s", exc)
            return []

    def _write_buffer(self, lines: list[str]) -> None:
        try:
            if lines:
                self._buffer.write_text("\n".join(lines) + "\n", encoding="utf-8")
            elif self._buffer.exists():
                self._buffer.unlink()
        except OSError as exc:
            log.error("could not rewrite buffer: %s", exc)

    def _append_buffer(self, envelope: dict[str, Any]) -> None:
        try:
            self._buffer.parent.mkdir(parents=True, exist_ok=True)
            with self._buffer.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(envelope, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.error("could not buffer %s: %s", envelope.get("msg_type"), exc)
            return
        self._trim_buffer()

    def _trim_buffer(self) -> None:
        lines = self._read_buffer()
        if len(lines) > _MAX_BUFFER_LINES:
            self._write_buffer(lines[-_MAX_BUFFER_LINES:])
