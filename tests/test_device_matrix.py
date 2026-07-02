"""get_identity_card: единая identity-карточка devices+printers+net_devices по
любому ключу (З.3). Read-only, fill-empty с приоритетом agent > printer > net.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import server.db as db
from tests.conftest import envelope, healthy


def _seed(tmp_path: Path) -> Path:
    p = tmp_path / "srp.db"
    db.init_db(p)
    return p


def test_identity_card_merges_three_tables(tmp_path: Path) -> None:
    _seed(tmp_path)
    db.upsert_device("dev-1", "2026-07-02T10:00:00+00:00", "0.1.0", hostname="PC-1")
    db.upsert_net_device(
        {
            "device_nid": "nd-1",
            "ip": "192.168.9.50",
            "mac": "AA-BB-CC-DD-EE-01",
            "dev_type": "agent",
        }
    )
    db.set_net_device_links("nd-1", device_id="dev-1")

    card = db.get_identity_card(device_id="dev-1")
    assert card is not None
    assert card["display_name"] == "PC-1"
    assert card["ip"] == "192.168.9.50"  # добрано из net_devices (agent не знает IP)
    assert card["net_nid"] == "nd-1"
    assert "agent" in card["sources"] and "net" in card["sources"]


def test_identity_card_agent_hostname_wins_over_net(tmp_path: Path) -> None:
    _seed(tmp_path)
    db.upsert_device("dev-2", "2026-07-02T10:00:00+00:00", "0.1.0", hostname="PC-2")
    db.upsert_net_device(
        {"device_nid": "nd-2", "hostname": "nd-guess-2", "mac": "AA-BB-CC-DD-EE-02"}
    )
    db.set_net_device_links("nd-2", device_id="dev-2")
    card = db.get_identity_card(device_id="dev-2")
    assert card is not None and card["hostname"] == "PC-2"


def test_identity_card_by_mac_and_ip(tmp_path: Path) -> None:
    _seed(tmp_path)
    db.upsert_device("dev-3", "2026-07-02T10:00:00+00:00", "0.1.0", hostname="PC-3")
    db.upsert_net_device({"device_nid": "nd-3", "ip": "192.168.9.51", "mac": "AA-BB-CC-DD-EE-03"})
    db.set_net_device_links("nd-3", device_id="dev-3")
    assert db.get_identity_card(mac="AA-BB-CC-DD-EE-03")["device_id"] == "dev-3"
    assert db.get_identity_card(ip="192.168.9.51")["device_id"] == "dev-3"


def test_identity_card_printer_only(tmp_path: Path) -> None:
    _seed(tmp_path)
    db.store_printer_reading(
        "prn-1", {"ip": "192.168.9.5", "hostname": "prn-host", "model": "LJ", "status": "online"}
    )
    card = db.get_identity_card(printer_id="prn-1")
    assert card is not None
    assert card["display_name"] == "prn-host"
    assert card["sources"] == ["printer"]


def test_identity_card_none_when_nothing_matches(tmp_path: Path) -> None:
    _seed(tmp_path)
    assert db.get_identity_card(ip="10.99.99.99") is None
    assert db.get_identity_card() is None


def test_fleet_does_not_duplicate_device_with_multiple_net_rows(tmp_path: Path) -> None:
    """get_devices() LEFT JOINs a per-device most-recent-net-row subquery -- a
    device linked from TWO net_devices rows (e.g. wired + wifi) must still
    appear exactly once, and the IP shown is the one from the row seen last
    (never an arbitrary MIN/MAX string pick that a stale NIC could win)."""
    _seed(tmp_path)
    db.upsert_device("dev-4", "2026-07-02T10:00:00+00:00", "0.1.0", hostname="PC-4")
    db.upsert_net_device(
        {"device_nid": "nd-4a", "ip": "192.168.9.99", "mac": "AA-00-00-00-00-04"},
        received_at="2026-07-01T09:00:00+00:00",  # older -> must lose
    )
    db.upsert_net_device(
        {"device_nid": "nd-4b", "ip": "192.168.9.61", "mac": "AA-00-00-00-00-05"},
        received_at="2026-07-02T09:00:00+00:00",  # newer -> must win
    )
    db.set_net_device_links("nd-4a", device_id="dev-4")
    db.set_net_device_links("nd-4b", device_id="dev-4")
    fleet = [d for d in db.get_devices() if d["device_id"] == "dev-4"]
    assert len(fleet) == 1
    assert fleet[0]["local_ip"] == "192.168.9.61"  # most-recently-seen net row wins
    # get_identity_card must agree with the fleet -- both use recency, not two
    # independently-invented "arbitrary first row" rules that could disagree.
    card = db.get_identity_card(device_id="dev-4")
    assert card is not None and card["ip"] == "192.168.9.61"


def test_fleet_local_ip_falls_back_to_net_devices_when_no_historical(tmp_path: Path) -> None:
    _seed(tmp_path)
    db.upsert_device("dev-5", "2026-07-02T10:00:00+00:00", "0.1.0", hostname="PC-5")
    db.upsert_net_device({"device_nid": "nd-5", "ip": "192.168.9.62", "mac": "AA-00-00-00-00-06"})
    db.set_net_device_links("nd-5", device_id="dev-5")
    row = next(d for d in db.get_devices() if d["device_id"] == "dev-5")
    assert row["local_ip"] == "192.168.9.62"


def test_fleet_display_name_disambiguates_unnamed_devices(tmp_path: Path) -> None:
    """Fleet is exactly the list where two blank names must stay tellable apart
    (e.g. the delete-confirm dialog) -- unlike other display_name call sites,
    the fleet row MUST pass disambiguate=True (review finding)."""
    _seed(tmp_path)
    db.upsert_device("dev-6", "2026-07-02T10:00:00+00:00", "0.1.0", hostname=None)
    db.upsert_device("dev-7", "2026-07-02T10:00:00+00:00", "0.1.0", hostname=None)
    rows = {d["device_id"]: d["display_name"] for d in db.get_devices()}
    assert rows["dev-6"].startswith(db.NEUTRAL_NAME) and "dev-6" in rows["dev-6"]
    assert rows["dev-7"].startswith(db.NEUTRAL_NAME) and "dev-7" in rows["dev-7"]
    assert rows["dev-6"] != rows["dev-7"]  # the whole point: distinguishable


# --------------------------------------------------------------------------- #
# GET /api/v1/device-card
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_device_card_api_by_device_id(client) -> None:
    client.post("/api/v1/ingest", json=envelope("dev-api1", "inventory", healthy("inventory")))
    r = client.get("/api/v1/device-card", params={"device_id": "dev-api1"})
    assert r.status_code == 200
    body = r.json()
    assert body["device_id"] == "dev-api1"
    assert "display_name" in body


@pytest.mark.integration
def test_device_card_api_404_when_not_found(client) -> None:
    r = client.get("/api/v1/device-card", params={"device_id": "nope"})
    assert r.status_code == 404


@pytest.mark.integration
def test_device_card_api_422_without_any_key(client) -> None:
    r = client.get("/api/v1/device-card")
    assert r.status_code == 422
