"""Silent printer discovery: source merge + config + read-side hints (phase 3).

Discovery unions three SILENT sources -- agent spooler-port hints, ARP snapshots
(already collected by the network collector), and the engineer's static config
list -- into one deduplicated candidate list WITHOUT scanning. Dedup precedence:
serial > MAC > IP (serial only appears later, at SNMP probe time). Privacy: every
candidate IP is re-checked RFC1918 server-side (defense in depth).
"""

from __future__ import annotations

import pytest
from server import db
from server.printers import discovery
from server.printers.config import PrinterConfig, load_printer_config

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_config_defaults_are_safe():
    cfg = load_printer_config(None)
    assert isinstance(cfg, PrinterConfig)
    assert cfg.active_scan is False  # active scan is OFF until explicitly enabled
    assert cfg.snmp_community == "public"
    assert cfg.static_ips == ()


@pytest.mark.unit
def test_config_static_ips_rfc1918_filtered():
    cfg = load_printer_config({"static_ips": ["192.168.1.5", "8.8.8.8", "10.0.0.9", "not-an-ip"]})
    assert cfg.static_ips == ("192.168.1.5", "10.0.0.9")


@pytest.mark.unit
def test_config_active_scan_only_true_when_explicit():
    assert load_printer_config({"active_scan": "yes"}).active_scan is False
    assert load_printer_config({"active_scan": True}).active_scan is True


@pytest.mark.unit
def test_config_interval_and_version_clamped():
    cfg = load_printer_config({"poll_interval_sec": 1, "snmp_version": 9})
    assert cfg.poll_interval_sec >= 60  # never hammer
    assert cfg.snmp_version in (0, 1)  # 0=v1, 1=v2c on the wire


# --------------------------------------------------------------------------- #
# Merge
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_one_printer_from_three_sources_is_one_candidate():
    agent_hints = [{"name": "HP LJ", "ip": "192.168.1.50"}]
    arp = [{"neighbors": [{"ip": "192.168.1.50", "mac": "AA-BB-CC-DD-EE-01"}]}]
    static = ["192.168.1.50"]
    out = discovery.merge(agent_hints=agent_hints, arp_snapshots=arp, static_ips=static)
    assert len(out) == 1
    cand = out[0]
    assert cand.ip == "192.168.1.50"
    assert cand.mac == "AA-BB-CC-DD-EE-01"
    assert cand.name == "HP LJ"
    assert set(cand.sources) == {"spooler", "arp", "config"}


@pytest.mark.unit
def test_public_ips_dropped_from_every_source():
    out = discovery.merge(
        agent_hints=[{"name": "x", "ip": "8.8.8.8"}],
        arp_snapshots=[{"neighbors": [{"ip": "1.1.1.1", "mac": "AA-BB-CC-DD-EE-02"}]}],
        static_ips=["9.9.9.9"],
    )
    assert out == []


@pytest.mark.unit
def test_same_mac_different_ip_collapses_to_one():
    # A printer that changed IP keeps one identity (MAC outranks IP).
    arp = [
        {
            "neighbors": [
                {"ip": "192.168.1.60", "mac": "AA-BB-CC-DD-EE-03"},
                {"ip": "192.168.1.61", "mac": "aa-bb-cc-dd-ee-03"},
            ]
        }
    ]
    out = discovery.merge(agent_hints=[], arp_snapshots=arp, static_ips=[])
    assert len(out) == 1
    assert out[0].mac == "AA-BB-CC-DD-EE-03"


@pytest.mark.unit
def test_empty_sources_give_empty_list():
    assert discovery.merge(agent_hints=[], arp_snapshots=[], static_ips=[]) == []


@pytest.mark.unit
def test_merge_is_deterministically_sorted():
    out = discovery.merge(
        agent_hints=[{"ip": "192.168.1.9"}, {"ip": "192.168.1.2"}],
        arp_snapshots=[],
        static_ips=[],
    )
    assert [c.ip for c in out] == ["192.168.1.2", "192.168.1.9"]


# --------------------------------------------------------------------------- #
# DB read side: latest printer-port hints per device
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_get_printer_port_hints_round_trips(client):
    from conftest import envelope, healthy

    hist = healthy("historical")
    hist["printer_ports"] = [{"name": "Shared HP", "ip": "192.168.1.77"}]
    resp = client.post("/api/v1/ingest", json=envelope("dev-printerports-1", "historical", hist))
    assert resp.status_code == 200, resp.text

    hints = db.get_printer_port_hints()
    assert {"name": "Shared HP", "ip": "192.168.1.77"} in hints


@pytest.mark.integration
def test_old_payload_without_hints_contributes_nothing(client):
    from conftest import envelope, healthy

    resp = client.post(
        "/api/v1/ingest", json=envelope("dev-noports-1", "historical", healthy("historical"))
    )
    assert resp.status_code == 200, resp.text
    assert db.get_printer_port_hints() == []
