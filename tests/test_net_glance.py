"""B5 (2026-08-27): «Сеть этой машины» на странице устройства, sys_descr на
карточке сетевого устройства (+ additive-миграция), журнал изменений /netmap/changes.

Read-only показ уже собранных данных: historical payload (network_adapters/
neighbors/routes/connections/printer_ports, shared/schema.py:184-307) и
net_devices/net_changes (server/db.py). Единственная миграция схемы во всём
плане -- net_devices.sys_descr, additive, по образцу _ADD_COLUMNS
([[netmap-identity-spine]]).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from server import db
from server.netdisco import oids, scheduler, snmp_probe
from server.netdisco.identity import device_nid
from server.netdisco.models import DeviceProfile

pytestmark = pytest.mark.integration


def _seed_device(device_id: str, hostname: str) -> None:
    db.touch_device(device_id, datetime.now(timezone.utc).isoformat(), "0.1.0", hostname=hostname)


def _seed_network(device_id: str, **fields) -> None:
    db.store_historical(device_id, datetime.now(timezone.utc).isoformat(), fields)


_ONE_ADAPTER = {
    "name": "Ethernet",
    "kind": "ethernet",
    "up": True,
    "ipv4": ["10.0.0.20"],
    "mac": "AA-BB-CC-DD-EE-01",
    "dns": ["10.0.0.1", "10.0.0.2"],
    "dhcp": True,
    "link_mbps": 1000.0,
    "gateway": "10.0.0.1",
}


# --------------------------------------------------------------------------- #
# 5.1 -- «Сеть этой машины»
# --------------------------------------------------------------------------- #
def test_net_glance_block_renders_collapsed_with_adapter_count(client: TestClient) -> None:
    _seed_device("dev-net1", "PC-NET1")
    _seed_network("dev-net1", network_adapters=[_ONE_ADAPTER])
    body = client.get("/device/dev-net1").text
    # Review fix (LOW, B5): "N адаптеров" is grammatically wrong for N=1 ("1
    # адаптеров") -- a neutral "адаптеров: N" label sidesteps RU plural forms.
    assert "Сеть этой машины · адаптеров: 1" in body
    # свёрнут по умолчанию -- у ЭТОГО <details> нет атрибута open
    i = body.find("Сеть этой машины · адаптеров: 1")
    details_start = body.rfind("<details", 0, i)
    tag_end = body.find(">", details_start)
    assert " open" not in body[details_start:tag_end]


def test_net_glance_adapter_shows_mac_dns_dhcp_speed(client: TestClient) -> None:
    _seed_device("dev-net2", "PC-NET2")
    _seed_network("dev-net2", network_adapters=[_ONE_ADAPTER])
    body = client.get("/device/dev-net2").text
    assert "AA-BB-CC-DD-EE-01" in body
    assert "10.0.0.1, 10.0.0.2" in body  # DNS-серверы через запятую
    assert "1000" in body  # link_mbps
    assert ">да<" in body  # dhcp: True -> "да"


def test_device_page_old_adapters_table_collapsed_not_duplicated(client: TestClient) -> None:
    """MEDIUM (review B1-B5 final): device.html's pre-existing "Сеть" table and
    the new "Сеть этой машины" block below it (_device_net.html) render the
    SAME hist.network_adapters list -- name/type/IP/gateway showed up twice in
    a row, unconditionally, on every device page. The older table now
    collapses into its own <details> (spec forbids deleting an existing view,
    only recomposition) instead of both staying always-open at once."""
    _seed_device("dev-net-dup", "PC-DUP")
    _seed_network("dev-net-dup", network_adapters=[_ONE_ADAPTER])
    body = client.get("/device/dev-net-dup").text
    assert "<summary>Адаптеры (1)</summary>" in body
    i = body.find("<summary>Адаптеры (1)</summary>")
    details_start = body.rfind("<details", 0, i)
    tag_end = body.find(">", details_start)
    assert " open" not in body[details_start:tag_end]  # свёрнут по умолчанию
    # the OLD table's own row is still rendered somewhere (spec forbids
    # deleting an existing view) -- it just no longer sits unconditionally
    # open next to the newer, more complete block below it.
    assert body.count("Ethernet") == 2  # old (now collapsed) table + new block


def test_net_glance_non_numeric_link_mbps_does_not_crash_page(client: TestClient) -> None:
    """LOW (review B5): historical payload is stored raw (pipeline.py passes
    env.payload straight to db.store_historical, no full pydantic model_dump),
    so link_mbps can arrive as a string. ``"%.0f"|format(mbps)`` raised
    TypeError on a str even when it looked numeric -- regression for
    _device_net.html:54."""
    _seed_device("dev-net-mbps", "PC-MBPS")
    _seed_network("dev-net-mbps", network_adapters=[{**_ONE_ADAPTER, "link_mbps": "1000"}])
    resp = client.get("/device/dev-net-mbps")
    assert resp.status_code == 200
    assert "1000" in resp.text


def test_net_glance_adapter_missing_optional_fields_show_dash(client: TestClient) -> None:
    """Sparse-фикстура (агент прислал не все поля) не должна падать: .get()
    everywhere -- регресс на живой баг B4 (dot-access + `is not none`)."""
    _seed_device("dev-net-sparse", "PC-SPARSE")
    _seed_network("dev-net-sparse", network_adapters=[{"name": "Wi-Fi"}])
    resp = client.get("/device/dev-net-sparse")
    assert resp.status_code == 200
    assert "Сеть этой машины · адаптеров: 1" in resp.text


def test_net_glance_neighbors_routes_printers_visible(client: TestClient) -> None:
    _seed_device("dev-net3", "PC-NET3")
    _seed_network(
        "dev-net3",
        network_adapters=[_ONE_ADAPTER],
        network_neighbors=[{"ip": "10.0.0.50", "mac": "00-11-22-33-44-55", "name": "OLDPC"}],
        network_routes=[{"dest": "10.5.0.0/16", "next_hop": "10.0.0.1", "metric": 10}],
        printer_ports=[{"name": "HP-OFFICE", "ip": "10.0.0.90"}],
    )
    body = client.get("/device/dev-net3").text
    assert "OLDPC" in body
    assert "10.5.0.0/16" in body
    assert "HP-OFFICE" in body


def test_net_glance_shows_no_data_not_not_found_when_key_absent(client: TestClient) -> None:
    """LOW (review B1-B5 final): hist.get('network_neighbors') is None (agent
    never sent the key -- pre-T2 agent, or that collector failed wholesale) is
    NOT the same fact as an empty list (collector ran, found nothing). The old
    `... or []` collapsed both into one «не обнаружено» claim -- the same
    false-confidence shape the B4 review fixed on the disk-errors chip
    (UNKNOWN over false confidence, CLAUDE.md §5)."""
    _seed_device("dev-net-absent", "PC-ABSENT")
    # network_neighbors/routes/printer_ports keys never sent at all.
    _seed_network("dev-net-absent", network_adapters=[_ONE_ADAPTER])
    body = client.get("/device/dev-net-absent").text
    assert "Нет данных о соседях в сети." in body
    assert "Соседей не обнаружено." not in body
    assert "Нет данных о маршрутах." in body
    assert "Дополнительных маршрутов нет." not in body
    assert "Нет данных о принтерах этого ПК." in body
    assert "Локальных принтеров не обнаружено." not in body


def test_net_glance_shows_not_found_when_key_present_but_empty(client: TestClient) -> None:
    """The collector-ran-but-found-nothing case must keep its existing wording."""
    _seed_device("dev-net-empty", "PC-EMPTY")
    _seed_network(
        "dev-net-empty",
        network_adapters=[_ONE_ADAPTER],
        network_neighbors=[],
        network_routes=[],
        printer_ports=[],
    )
    body = client.get("/device/dev-net-empty").text
    assert "Соседей не обнаружено." in body
    assert "Нет данных о соседях в сети." not in body
    assert "Дополнительных маршрутов нет." in body
    assert "Нет данных о маршрутах." not in body
    assert "Локальных принтеров не обнаружено." in body
    assert "Нет данных о принтерах этого ПК." not in body


def test_net_glance_tcp_connections_in_nested_details(client: TestClient) -> None:
    _seed_device("dev-net4", "PC-NET4")
    _seed_network(
        "dev-net4",
        network_adapters=[_ONE_ADAPTER],
        network_connections=[
            {
                "local_ip": "10.0.0.20",
                "local_port": 51000,
                "remote_ip": "1.2.3.4",
                "remote_port": 443,
                "state": "ESTABLISHED",
            }
        ],
    )
    body = client.get("/device/dev-net4").text
    assert "<summary>TCP-соединения (1)</summary>" in body
    assert "ESTABLISHED" in body


def test_net_glance_tcp_and_printer_headers_have_title_tooltips(client: TestClient) -> None:
    """LOW (review B5): plan Global Constraints require a title tooltip on
    every new metric -- the TCP-соединения headers and the printer-ports IP
    column were missing theirs (_device_net.html:118,137)."""
    _seed_device("dev-net-titles", "PC-TITLES")
    _seed_network(
        "dev-net-titles",
        network_adapters=[_ONE_ADAPTER],
        network_connections=[
            {"local_ip": "10.0.0.20", "remote_ip": "1.2.3.4", "state": "ESTABLISHED"}
        ],
        printer_ports=[{"name": "HP-OFFICE", "ip": "10.0.0.90"}],
    )
    body = client.get("/device/dev-net-titles").text
    assert 'title="Локальный IP-адрес и порт этого ПК"' in body
    assert 'title="IP-адрес и порт удалённой стороны соединения"' in body
    assert 'title="Состояние TCP-соединения' in body
    assert 'title="IP-адрес принтера в локальной сети"' in body


def test_net_glance_absent_without_network_data(client: TestClient) -> None:
    _seed_device("dev-net5", "PC-NET5")
    body = client.get("/device/dev-net5").text
    assert "Сеть этой машины" not in body


def test_net_glance_absent_when_no_historical_row_at_all(client: TestClient) -> None:
    """d.historical is None (устройство известно, но телеметрии ещё не было) --
    блок не падает, а просто отсутствует."""
    _seed_device("dev-net-nohist", "PC-NOHIST")
    resp = client.get("/device/dev-net-nohist")
    assert resp.status_code == 200
    assert "Сеть этой машины" not in resp.text


# --------------------------------------------------------------------------- #
# 5.2 -- net_device.html: if_alias + sys_descr (+ миграция)
# --------------------------------------------------------------------------- #
def test_net_device_interfaces_show_if_alias(client: TestClient) -> None:
    db.upsert_net_device(
        {"device_nid": "nd-sw2", "ip": "10.0.0.2", "hostname": "sw2", "dev_type": "switch"}
    )
    db.store_net_interfaces(
        "nd-sw2", [{"if_index": 1, "name": "Gi0/1", "if_alias": "uplink-to-core"}]
    )
    body = client.get("/netdisco/device/nd-sw2").text
    assert "uplink-to-core" in body


class _FakeIfaceSession:
    """Minimal SNMP session stub for _build_interfaces: answers only the
    IF_ALIAS walk (one interface, index 1); every other interface-table walk
    comes back empty (_build_interfaces must tolerate that -- same shape as
    the sparse-fixture net_glance tests above)."""

    def __init__(self, alias: str) -> None:
        self._alias = alias

    def get(self, oid_list):
        return {}

    def walk(self, base_oid, *, max_rows=512):
        if base_oid == oids.IF_ALIAS:
            return {f"{oids.IF_ALIAS}.1": self._alias}
        return {}


def test_build_interfaces_drops_nonprintable_if_alias() -> None:
    """LOW (review B1-B5 final): if_alias got the same length cap as
    sys_descr/model_name (snmp_probe.py:30,131) but never the isprintable()
    hygiene those two got in the B5 review fix (commit 1f74401) -- a hostile
    SNMP responder can plant control bytes / a bidi-override in ifAlias and
    have it render verbatim on the net-device card (autoescape stops HTML
    injection, not glyph-order spoofing)."""
    ifaces = snmp_probe._build_interfaces(_FakeIfaceSession("uplink\x00\x1b[bidi]"))
    assert len(ifaces) == 1
    assert ifaces[0].if_alias is None


def test_build_interfaces_keeps_printable_if_alias() -> None:
    ifaces = snmp_probe._build_interfaces(_FakeIfaceSession("uplink to core"))
    assert ifaces[0].if_alias == "uplink to core"


def test_device_update_maps_and_caps_sys_descr() -> None:
    profile = DeviceProfile(ip="10.0.0.9", responded=True, sys_descr="X" * 500)
    row = scheduler._device_update("nd-x", profile, "switch", {})
    assert row["sys_descr"] is not None
    assert len(row["sys_descr"]) == 256


def test_device_update_caps_model_fallback_from_sys_descr() -> None:
    """LOW (review B5): the model column's sysDescr fallback is the SAME
    untrusted banner as sys_descr -- it must get the SAME 256-char cap, or the
    cap on sys_descr is trivially bypassed by reading the banner back out of
    model instead (scheduler.py:359)."""
    profile = DeviceProfile(ip="10.0.0.9", responded=True, sys_descr="X" * 500)
    row = scheduler._device_update("nd-x", profile, "switch", {})
    assert row["model"] is not None
    assert len(row["model"]) == 256


def test_device_update_caps_model_from_profile_model_name() -> None:
    """MEDIUM (review B1-B5 final): scheduler.py:351-354's own comment claims
    "the model column can't be used to smuggle an unbounded string past the
    cap already applied to sys_descr" -- but that cap only ever applied to the
    sysDescr FALLBACK. profile.model_name (ENTITY-MIB entPhysicalModelName,
    _first_str(walk(...)), no length bound of its own) is a HIGHER-priority
    source and still reached net_devices.model raw."""
    profile = DeviceProfile(ip="10.0.0.9", responded=True, model_name="M" * 500)
    row = scheduler._device_update("nd-y", profile, "switch", {})
    assert row["model"] is not None
    assert len(row["model"]) == 256


def test_device_update_caps_model_from_extras_vendor_driver() -> None:
    """Same cap must apply to the HIGHEST-priority source too (a vendor
    driver's own "model" reading, e.g. from an HTTP/JSON API -- unbounded)."""
    profile = DeviceProfile(ip="10.0.0.9", responded=True)
    row = scheduler._device_update("nd-z", profile, "switch", {"model": "V" * 500})
    assert row["model"] is not None
    assert len(row["model"]) == 256


def test_device_update_sys_descr_none_when_profile_has_none() -> None:
    profile = DeviceProfile(ip="10.0.0.9", responded=True, sys_descr=None)
    row = scheduler._device_update("nd-x", profile, "switch", {})
    assert row["sys_descr"] is None


class _FakeSysDescrSession:
    """Minimal SNMP session stub: answers only sysDescr/sysName, no table walks
    (probe_device still needs `get()`/`walk()` -- see snmp_probe.Session)."""

    def __init__(self, sys_descr: str) -> None:
        self._sys_descr = sys_descr

    def get(self, oid_list):
        return {oids.SYS_DESCR: self._sys_descr, oids.SYS_NAME: "sw1"}

    def walk(self, base_oid, *, max_rows=512):
        return {}


def test_probe_drops_nonprintable_sys_descr() -> None:
    """LOW (review B5): sys_descr must get the same printability hygiene as
    model_name (snmp_probe.py:166) -- a hostile SNMP-answering host can plant
    control bytes / a bidi-override in the free-text banner otherwise, and it
    reaches the operator card as-is (autoescape stops XSS, not spoofing)."""
    prof = snmp_probe.probe_device("10.0.0.9", _FakeSysDescrSession("Cisco\x00\x1b[bidi]"))
    assert prof.sys_descr is None


def test_probe_keeps_printable_sys_descr() -> None:
    prof = snmp_probe.probe_device("10.0.0.9", _FakeSysDescrSession("Cisco IOS Software, C2960"))
    assert prof.sys_descr == "Cisco IOS Software, C2960"


def test_sys_descr_persists_and_shown_on_card(client: TestClient) -> None:
    db.upsert_net_device(
        {
            "device_nid": "nd-descr1",
            "ip": "10.0.0.3",
            "hostname": "sw3",
            "dev_type": "switch",
            "sys_descr": "Cisco IOS Software, C2960",
        }
    )
    assert db.get_net_device("nd-descr1")["sys_descr"] == "Cisco IOS Software, C2960"
    body = client.get("/netdisco/device/nd-descr1").text
    assert "Описание (SNMP)" in body
    assert "Cisco IOS Software, C2960" in body


def test_sys_descr_absent_shows_no_description_row(client: TestClient) -> None:
    db.upsert_net_device(
        {"device_nid": "nd-nodescr", "ip": "10.0.0.4", "hostname": "sw4", "dev_type": "switch"}
    )
    body = client.get("/netdisco/device/nd-nodescr").text
    assert "Описание (SNMP)" not in body


def test_net_devices_sys_descr_column_added_on_legacy_db(tmp_path) -> None:
    """Additive-миграция: старая БД (без sys_descr) открывается без падения, и
    колонка становится рабочей -- идиома _ADD_COLUMNS ([[netmap-identity-spine]])."""
    path = tmp_path / "legacy_net.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        """CREATE TABLE net_devices (
             device_nid TEXT PRIMARY KEY, ip TEXT, hostname TEXT, mac TEXT, vendor TEXT,
             dev_type TEXT, sys_object_id TEXT, model TEXT, serial TEXT, site_code TEXT,
             status TEXT, subtype TEXT, first_seen TEXT, last_seen TEXT,
             device_id TEXT, printer_id TEXT, snmp_mute_until TEXT
           )"""
    )
    conn.execute("INSERT INTO net_devices (device_nid, ip) VALUES ('nd-old', '10.0.0.5')")
    conn.commit()
    conn.close()

    db.init_db(path)  # должно ALTER TABLE ... ADD COLUMN sys_descr, без падения

    row = db.get_net_device("nd-old")
    assert row is not None
    assert row["sys_descr"] is None  # старая строка: без бэкафилла, но и без падения
    db.upsert_net_device({"device_nid": "nd-old", "sys_descr": "now known"})
    assert db.get_net_device("nd-old")["sys_descr"] == "now known"


def test_merge_preserves_sys_descr_from_old_row(client: TestClient) -> None:
    """MEDIUM (review B5): the merge branch of _rename_or_merge_net_device_row
    (db.py:2120) listed every identity column except the new sys_descr one --
    an SNMP description recorded on an IP-keyed row was silently dropped the
    moment the same host got re-observed by MAC and folded into that identity."""
    mac = "AA:BB:CC:DD:EE:02"
    mac_nid = device_nid(mac=mac)
    ip = "10.0.0.77"
    ip_nid = device_nid(ip=ip)
    db.upsert_net_device({"device_nid": mac_nid, "mac": mac, "dev_type": "switch"})
    db.upsert_net_device({"device_nid": ip_nid, "ip": ip, "sys_descr": "Cisco IOS Software, C2960"})

    db.upsert_net_device({"device_nid": mac_nid, "mac": mac, "ip": ip, "dev_type": "switch"})

    assert db.get_net_device(ip_nid) is None  # old IP-only identity folded away
    assert db.get_net_device(mac_nid)["sys_descr"] == "Cisco IOS Software, C2960"


# --------------------------------------------------------------------------- #
# 5.3 -- /netmap/changes
# --------------------------------------------------------------------------- #
def test_netmap_changes_page_ok_and_shows_fixture_change(client: TestClient) -> None:
    db.store_net_change("device_new", device_nid="nd-chg1", detail={})
    resp = client.get("/netmap/changes")
    assert resp.status_code == 200
    assert "появилось устройство" in resp.text
    assert "nd-chg1" in resp.text


def test_netmap_changes_unknown_kind_shown_as_raw_text(client: TestClient) -> None:
    """Fail-open: неизвестный (RU-словарём непокрытый) тип не прячется."""
    db.store_net_change("appeared", device_nid="nd-chg2", detail={})
    body = client.get("/netmap/changes").text
    assert "appeared" in body


def test_netmap_changes_default_period_is_30_days(client: TestClient) -> None:
    body = client.get("/netmap/changes").text
    assert "<strong>30 дн</strong>" in body


def test_netmap_changes_period_selector_switches(client: TestClient) -> None:
    body = client.get("/netmap/changes?days=90").text
    assert "<strong>90 дн</strong>" in body


def test_netmap_changes_invalid_days_falls_back_to_default(client: TestClient) -> None:
    resp = client.get("/netmap/changes?days=999")
    assert resp.status_code == 200
    assert "<strong>30 дн</strong>" in resp.text


def test_netmap_changes_excludes_rows_outside_period(client: TestClient) -> None:
    db.store_net_change(
        "device_new", device_nid="nd-old-chg", detail={}, ts="2000-01-01T00:00:00+00:00"
    )
    body = client.get("/netmap/changes?days=7").text
    assert "nd-old-chg" not in body


def test_netmap_changes_link_shows_edge_endpoints(client: TestClient) -> None:
    db.store_net_change("link_removed", device_nid=None, detail={"a": "nd-a1", "b": "nd-b1"})
    body = client.get("/netmap/changes").text
    assert "nd-a1" in body and "nd-b1" in body


def test_netmap_panel_links_to_changes_page(client: TestClient) -> None:
    body = client.get("/netmap").text
    assert 'href="/netmap/changes"' in body
