"""Имена узлов Zabbix: суффикс, соответствие, дубли, отбор и пропуски."""

from __future__ import annotations

from datetime import datetime, timezone

from server.zabbix import items as it
from server.zabbix.config import ZabbixConfig

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
CFG_Z = ZabbixConfig(server="z")


def row(device_id="dev-1", hostname="BUH-01", org_code="7", **over) -> dict:
    base = {
        "device_id": device_id,
        "hostname": hostname,
        "model": "Latitude 5420",
        "chassis": "laptop",
        "org_code": org_code,
        "score_ts": NOW.isoformat(),
        "state": "h1",
        "risk": 12.0,
        "days_left": None,
        "dominant_label": "Накопитель",
        "action": "наблюдать",
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------- #
# Как выбирается имя
# --------------------------------------------------------------------------- #
def test_hostname_is_the_default_name():
    assert it.resolve_host(row(), ZabbixConfig(server="z")) == "BUH-01"


def test_suffix_turns_a_short_name_into_an_fqdn():
    cfg = ZabbixConfig(server="z", host_suffix=".corp.local")

    assert it.resolve_host(row(), cfg) == "BUH-01.corp.local"


def test_device_id_can_be_the_name_for_parks_that_prefer_opaque_ids():
    cfg = ZabbixConfig(server="z", host_field="device_id")

    assert it.resolve_host(row(), cfg) == "dev-1"


def test_display_name_falls_back_to_the_model_when_hostname_is_empty():
    cfg = ZabbixConfig(server="z", host_field="display_name")

    assert it.resolve_host(row(hostname=""), cfg) == "Latitude 5420"


def test_host_map_wins_over_everything_else():
    cfg = ZabbixConfig(server="z", host_suffix=".corp", host_map={"dev-1": "INV-4471"})

    assert it.resolve_host(row(), cfg) == "INV-4471"


def test_no_usable_name_returns_none():
    cfg = ZabbixConfig(server="z")

    assert it.resolve_host(row(hostname="   "), cfg) is None


# --------------------------------------------------------------------------- #
# Сборка пакета по парку
# --------------------------------------------------------------------------- #
def test_machine_without_a_name_is_skipped_with_a_reason():
    packet = it.build_packet([row(hostname="")], ZabbixConfig(server="z"), now=NOW)

    assert packet.items == []
    assert [s.reason for s in packet.skipped] == [it.SKIP_NO_NAME]
    assert "имя узла" in packet.skipped[0].label


def test_two_machines_with_the_same_name_are_both_skipped_not_merged():
    """Молчаливая склейка двух ПК на одном узле хуже пропуска."""
    rows = [row(device_id="dev-1"), row(device_id="dev-2")]

    packet = it.build_packet(rows, ZabbixConfig(server="z"), now=NOW)

    assert packet.hosts == []
    assert {s.device_id for s in packet.skipped} == {"dev-1", "dev-2"}
    assert {s.reason for s in packet.skipped} == {it.SKIP_DUPLICATE_NAME}


def test_distinct_names_are_all_exported():
    rows = [row(device_id="dev-1", hostname="A"), row(device_id="dev-2", hostname="B")]

    packet = it.build_packet(rows, ZabbixConfig(server="z"), now=NOW)

    assert sorted(h for h, _d in packet.hosts) == ["A", "B"]
    assert packet.skipped == []


def test_org_filter_selects_a_pilot_subset():
    rows = [
        row(device_id="dev-1", hostname="A", org_code="7"),
        row(device_id="dev-2", hostname="B", org_code="9"),
    ]

    packet = it.build_packet(rows, ZabbixConfig(server="z", org_codes=("7",)), now=NOW)

    assert [h for h, _d in packet.hosts] == ["A"]


def test_empty_org_filter_means_the_whole_fleet():
    rows = [
        row(device_id="dev-1", hostname="A", org_code="7"),
        row(device_id="dev-2", hostname="B"),
    ]

    packet = it.build_packet(rows, ZabbixConfig(server="z"), now=NOW)

    assert len(packet.hosts) == 2


def test_backoff_hosts_are_skipped_with_their_own_reason():
    rows = [row(device_id="dev-1", hostname="A"), row(device_id="dev-2", hostname="B")]

    packet = it.build_packet(rows, ZabbixConfig(server="z"), now=NOW, backoff_hosts={"B"})

    assert [h for h, _d in packet.hosts] == ["A"]
    assert [s.reason for s in packet.skipped] == [it.SKIP_BACKOFF]


def test_unknown_machines_are_counted_separately():
    rows = [
        row(device_id="dev-1", hostname="A", state="unknown"),
        row(device_id="dev-2", hostname="B", state="h0"),
    ]

    packet = it.build_packet(rows, ZabbixConfig(server="z"), now=NOW)

    assert packet.unknown == 1


def test_packet_items_carry_the_resolved_host_name():
    cfg = ZabbixConfig(server="z", host_suffix=".corp")

    packet = it.build_packet([row()], cfg, now=NOW)

    assert {i.host for i in packet.items} == {"BUH-01.corp"}


# --------------------------------------------------------------------------- #
# Имя узла приходит из недоверенной строки (находка security-ревью)
# --------------------------------------------------------------------------- #
def test_control_characters_cannot_forge_log_lines():
    """hostname приходит из /ingest, который в поставке не аутентифицирован."""
    forged = it.resolve_host(row(hostname="BUH-01\nWARNING поддельная строка"), CFG_Z)

    assert forged is not None
    assert "\n" not in forged


def test_host_name_length_is_capped_to_the_zabbix_limit():
    capped = it.resolve_host(row(hostname="A" * 5000), CFG_Z)

    assert capped is not None
    assert len(capped) <= 128


def test_name_of_only_control_characters_is_no_name_at_all():
    assert it.resolve_host(row(hostname="\x00\x01\x02"), CFG_Z) is None


def test_suffix_cannot_push_the_name_over_the_cap():
    long_suffix = ZabbixConfig(server="z", host_suffix="." + "b" * 300)

    assert len(it.resolve_host(row(), long_suffix)) <= 128


def test_host_map_value_is_sanitised_too():
    cfg = ZabbixConfig(server="z", host_map={"dev-1": "INV\n4471"})

    assert "\n" not in it.resolve_host(row(), cfg)
