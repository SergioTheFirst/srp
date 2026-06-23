"""printer_ip_map: server-side (device, queue-name) -> printer IP resolution (printview Phase 1).

The agent ships spooler ``{name -> RFC1918 ip}`` hints inside HistoricalPayload
(``printer_ports``). On every ``historical`` ingest the server upserts them into
``printer_ip_map`` so print views resolve a print job's queue name to its printer
IP with a plain JOIN -- no agent or contract change. Public IPs / hostnames /
queues without a TCP/IP port stay unresolved (NULL), honestly.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from tests.conftest import envelope, healthy

pytestmark = pytest.mark.integration


def _hist_with_ports(ports: list) -> dict:
    payload = healthy("historical")
    payload["printer_ports"] = ports
    return payload


# --------------------------------------------------------------------------- #
# Pure helpers (no DB)
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_is_rfc1918_accepts_private_rejects_public() -> None:
    from server.db import _is_rfc1918

    assert _is_rfc1918("192.168.1.1")
    assert _is_rfc1918("10.0.0.5")
    assert _is_rfc1918("172.16.5.5")
    assert not _is_rfc1918("8.8.8.8")
    assert not _is_rfc1918("203.0.113.7")
    assert not _is_rfc1918("not-an-ip")
    assert not _is_rfc1918("")
    assert not _is_rfc1918(None)


@pytest.mark.unit
def test_clean_port_hint_validates() -> None:
    from server.db import _clean_port_hint

    assert _clean_port_hint({"name": "Q", "ip": "192.168.1.2"}) == ("Q", "192.168.1.2")
    assert _clean_port_hint({"name": "Q", "ip": " 10.0.0.1 "}) == ("Q", "10.0.0.1")
    assert _clean_port_hint({"name": "Q", "ip": "8.8.8.8"}) is None
    assert _clean_port_hint({"name": "", "ip": "192.168.1.2"}) is None
    assert _clean_port_hint({"ip": "192.168.1.2"}) is None
    assert _clean_port_hint({"name": "Q", "ip": None}) is None
    assert _clean_port_hint("nope") is None


@pytest.mark.unit
def test_clean_port_hint_truncates_long_name() -> None:
    from server.db import _PRINTER_NAME_CAP, _clean_port_hint

    cleaned = _clean_port_hint({"name": "x" * 500, "ip": "192.168.1.2"})
    assert cleaned is not None
    assert len(cleaned[0]) == _PRINTER_NAME_CAP


# --------------------------------------------------------------------------- #
# Ingest hook -> map populated
# --------------------------------------------------------------------------- #


def test_historical_ingest_populates_printer_ip_map(client: TestClient) -> None:
    from server import db

    ports = [{"name": "HP LaserJet", "ip": "192.168.1.50"}]
    r = client.post("/api/v1/ingest", json=envelope("dev-1", "historical", _hist_with_ports(ports)))
    assert r.status_code == 200, r.text
    assert db.get_printer_ip("dev-1", "HP LaserJet") == "192.168.1.50"


def test_public_ip_hint_is_not_stored(client: TestClient) -> None:
    from server import db

    # A public IP that somehow reaches the server (direct poster, not the agent)
    # must be rejected server-side: UNKNOWN over a wrong mapping.
    ports = [{"name": "Bad", "ip": "8.8.8.8"}]
    client.post("/api/v1/ingest", json=envelope("dev-2", "historical", _hist_with_ports(ports)))
    assert db.get_printer_ip("dev-2", "Bad") is None


def test_wsd_queue_without_ip_resolves_none(client: TestClient) -> None:
    from server import db

    ports = [{"name": "WSD-Printer", "ip": None}]
    client.post("/api/v1/ingest", json=envelope("dev-3", "historical", _hist_with_ports(ports)))
    assert db.get_printer_ip("dev-3", "WSD-Printer") is None


def test_reingest_updates_ip_in_place(client: TestClient) -> None:
    from server import db

    client.post(
        "/api/v1/ingest",
        json=envelope(
            "dev-4", "historical", _hist_with_ports([{"name": "Q", "ip": "192.168.1.10"}])
        ),
    )
    client.post(
        "/api/v1/ingest",
        json=envelope(
            "dev-4", "historical", _hist_with_ports([{"name": "Q", "ip": "192.168.1.11"}])
        ),
    )
    assert db.get_printer_ip("dev-4", "Q") == "192.168.1.11"
    rows = [m for m in db.iter_printer_port_map() if m["device_id"] == "dev-4"]
    assert rows == [{"device_id": "dev-4", "name": "Q", "ip": "192.168.1.11"}]


def test_get_printer_ip_unknown_pair_is_none(client: TestClient) -> None:
    from server import db

    assert db.get_printer_ip("ghost", "no-such-queue") is None


def test_store_printer_ip_hints_rejects_non_list(client: TestClient) -> None:
    from server import db

    # Defense-in-depth: a malformed direct poster could send a non-list; the
    # server must write nothing and never crash (env.payload is the raw dict).
    assert db.store_printer_ip_hints("dev-x", "oops") == 0  # type: ignore[arg-type]
    assert db.store_printer_ip_hints("dev-x", []) == 0
    assert db.store_printer_ip_hints("", [{"name": "Q", "ip": "192.168.1.1"}]) == 0


# --------------------------------------------------------------------------- #
# Device cleanup + backfill
# --------------------------------------------------------------------------- #


def test_delete_device_clears_printer_ip_map(client: TestClient) -> None:
    from server import db

    client.post(
        "/api/v1/ingest",
        json=envelope("dev-5", "historical", _hist_with_ports([{"name": "P", "ip": "10.1.2.3"}])),
    )
    assert db.get_printer_ip("dev-5", "P") == "10.1.2.3"
    db.delete_device("dev-5")
    assert db.get_printer_ip("dev-5", "P") is None


def test_backfill_populates_from_existing_historical(client: TestClient) -> None:
    from server import db

    # Store historical directly (bypassing the ingest hook) to simulate data that
    # predates the map -> the map is empty until backfill rebuilds it.
    db.store_historical(
        "dev-6",
        "2026-06-01T00:00:00+00:00",
        {"printer_ports": [{"name": "BF", "ip": "172.16.0.9"}]},
    )
    assert db.get_printer_ip("dev-6", "BF") is None
    db.backfill_printer_ip_map()
    assert db.get_printer_ip("dev-6", "BF") == "172.16.0.9"


def test_backfill_uses_newest_historical_per_device(client: TestClient) -> None:
    from server import db

    db.store_historical(
        "dev-7", "2026-06-01T00:00:00+00:00", {"printer_ports": [{"name": "Q", "ip": "10.0.0.1"}]}
    )
    db.store_historical(
        "dev-7", "2026-06-02T00:00:00+00:00", {"printer_ports": [{"name": "Q", "ip": "10.0.0.2"}]}
    )
    db.backfill_printer_ip_map()
    assert db.get_printer_ip("dev-7", "Q") == "10.0.0.2"
