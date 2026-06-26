"""Ф6 (network-map unification): ONE canonical card per physical device.

A network device that is FK-linked (Ф1) to an SRP agent or a printer must not
own a second card: ``/netdisco/device/{nid}`` of a linked node 302-redirects to
the canonical agent/printer page, and that page embeds the topology section.
Standalone infrastructure (router/switch with no agent/printer twin) keeps its
own net card. Card-url priority is agent > printer > net-infra.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


def _seed_net_device(nid, ip, hostname, dev_type="switch", status="up"):
    from server import db

    db.upsert_net_device(
        {
            "device_nid": nid,
            "ip": ip,
            "hostname": hostname,
            "mac": "AA-BB-CC-00-00-11",
            "vendor": "Cisco",
            "dev_type": dev_type,
            "status": status,
        }
    )


# --------------------------------------------------------------------------- #
# T1 -- card_url resolver priority (unit)
# --------------------------------------------------------------------------- #
def test_card_url_priority_agent_over_printer_over_net():
    from server.netdisco.unified import _card_url

    assert _card_url("dev-1", "prn-1", "nd-x") == "/device/dev-1"  # agent wins
    assert _card_url(None, "prn-1", "nd-x") == "/printers/prn-1"  # printer next
    assert _card_url(None, None, "nd-x") == "/netdisco/device/nd-x"  # net-infra
    assert _card_url(None, None, "nd-unknown") is None  # the bucket has no card
    assert _card_url(None, None, "") is None


# --------------------------------------------------------------------------- #
# T2/T3 backend -- reverse FK lookup
# --------------------------------------------------------------------------- #
def test_get_linked_net_device_by_agent_and_printer(client):
    from server import db

    _seed_net_device("nd-a", "192.168.1.10", "pc-switchport")
    _seed_net_device("nd-p", "192.168.1.20", "printer-net", dev_type="printer")
    db.set_net_device_links("nd-a", device_id="agent-1")
    db.set_net_device_links("nd-p", printer_id="prn-1")

    a = db.get_linked_net_device(device_id="agent-1")
    assert a is not None and a["device_nid"] == "nd-a"
    assert "interfaces" in a and "links" in a  # full card payload

    p = db.get_linked_net_device(printer_id="prn-1")
    assert p is not None and p["device_nid"] == "nd-p"

    assert db.get_linked_net_device(device_id="nobody") is None
    assert db.get_linked_net_device(printer_id="nobody") is None
    assert db.get_linked_net_device() is None  # no key -> no match


# --------------------------------------------------------------------------- #
# T4 -- /netdisco/device/{nid} redirects for linked nodes
# --------------------------------------------------------------------------- #
def test_linked_net_device_redirects_to_agent_card(client):
    from server import db

    _seed_net_device("nd-a", "192.168.1.10", "pc-switchport")
    db.set_net_device_links("nd-a", device_id="agent-1")

    r = client.get("/netdisco/device/nd-a", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/device/agent-1"


def test_linked_net_device_redirects_to_printer_card(client):
    from server import db

    _seed_net_device("nd-p", "192.168.1.20", "printer-net", dev_type="printer")
    db.set_net_device_links("nd-p", printer_id="prn-1")

    r = client.get("/netdisco/device/nd-p", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/printers/prn-1"


def test_standalone_infra_net_device_still_renders(client):
    """Pure infra (no agent/printer twin) keeps its own net card -- no redirect."""
    _seed_net_device("nd-sw", "192.168.1.2", "core-switch", dev_type="switch")
    r = client.get("/netdisco/device/nd-sw", follow_redirects=False)
    assert r.status_code == 200
    assert "core-switch" in r.text


# --------------------------------------------------------------------------- #
# T2/T3 -- canonical cards embed the topology section
# --------------------------------------------------------------------------- #
def test_device_page_embeds_net_topology_section(seeded_client):
    from server import db
    from tests.conftest import HEALTHY_DEVICE

    _seed_net_device("nd-a", "192.168.1.10", "pc-switchport")
    db.store_net_interfaces(
        "nd-a", [{"if_index": 1, "name": "GigabitEthernet0/5", "if_type": 6, "oper_up": 1}]
    )
    db.set_net_device_links("nd-a", device_id=HEALTHY_DEVICE)

    body = seeded_client.get(f"/device/{HEALTHY_DEVICE}").text
    assert "Сеть — топология" in body
    assert "GigabitEthernet0/5" in body  # interface row from linked net device
    assert "/netdisco/device/nd-a" in body  # link to full net card


def test_printer_page_embeds_net_topology_section(client):
    from server import db

    db.store_printer_reading("prn-1", {"model": "HP LJ", "status": "online"})
    _seed_net_device("nd-p", "192.168.1.20", "printer-net", dev_type="printer")
    db.store_net_interfaces("nd-p", [{"if_index": 1, "name": "eth0", "if_type": 6, "oper_up": 1}])
    db.set_net_device_links("nd-p", printer_id="prn-1")

    body = client.get("/printers/prn-1").text
    assert "Сеть — топология" in body
    assert "/netdisco/device/nd-p" in body


def test_unlinked_device_page_has_no_topology_section(seeded_client):
    from tests.conftest import HEALTHY_DEVICE

    body = seeded_client.get(f"/device/{HEALTHY_DEVICE}").text
    assert "Сеть — топология" not in body  # nothing linked -> section absent
