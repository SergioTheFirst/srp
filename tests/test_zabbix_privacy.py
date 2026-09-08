"""Приватность экспорта: что НЕ должно оказаться в байтах пакета.

Тест формулируется так же, как остальные приватные тесты проекта: не «внутреннее
состояние правильное», а «этой строки нет в исходящем артефакте». Артефакт здесь —
сырой ZBXD-пакет, который уйдёт в чужую систему.

Проверяется настоящий путь: реальная база (seeded_client), реальный запрос
db.get_fleet_verdicts(), реальная сборка пакета.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from server import db
from server.zabbix import items as it
from server.zabbix import protocol
from server.zabbix.config import ZabbixConfig

from tests.conftest import HEALTHY_DEVICE

# Значения, которые сажаются в базу и не должны уехать наружу ни при каких
# настройках экспорта.
OWNER_NAME = "Иванова Мария Петровна"
OWNER_POSITION = "главный бухгалтер"
OWNER_PHONE = "+7 999 123-45-67"
OPERATOR_COMMENT = "стоит в кабинете 214, ключ у охраны"
ORG_NAME = "ООО «Ромашка-Прим»"
SERIAL_HASH = "feedface00001111"


def _seed_sensitive(client) -> None:
    with db._connect() as conn:  # noqa: SLF001 -- тесту нужна прямая посадка данных
        conn.execute(
            """
            UPDATE devices
               SET owner_full_name = ?, owner_position = ?, owner_phone = ?,
                   comment = ?, org_code = ?, site_name = ?
             WHERE device_id = ?
            """,
            (
                OWNER_NAME,
                OWNER_POSITION,
                OWNER_PHONE,
                OPERATOR_COMMENT,
                "7",
                ORG_NAME,
                HEALTHY_DEVICE,
            ),
        )


def _packet_bytes(cfg: ZabbixConfig) -> bytes:
    rows = db.get_fleet_verdicts()
    assert rows, "в базе должен быть хотя бы один вердикт"
    packet = it.build_packet(rows, cfg, now=datetime.now(timezone.utc))
    raw = protocol.build_packet(packet.items, clock=1, ns=1)
    assert raw is not None
    return raw


def test_owner_personal_data_never_reaches_the_wire(seeded_client):
    _seed_sensitive(seeded_client)

    raw = _packet_bytes(ZabbixConfig(server="10.0.0.5"))

    for secret in (OWNER_NAME, OWNER_POSITION, OWNER_PHONE, OPERATOR_COMMENT):
        assert secret.encode("utf-8") not in raw, f"в пакет утекло: {secret}"


def test_organisation_name_never_reaches_the_wire(seeded_client):
    _seed_sensitive(seeded_client)

    raw = _packet_bytes(ZabbixConfig(server="10.0.0.5"))

    assert ORG_NAME.encode("utf-8") not in raw


def test_disk_serial_hash_never_reaches_the_wire(seeded_client):
    _seed_sensitive(seeded_client)

    raw = _packet_bytes(ZabbixConfig(server="10.0.0.5"))

    assert SERIAL_HASH.encode("utf-8") not in raw


def test_no_ip_address_reaches_the_wire(seeded_client):
    """В пакете нет ни одного адресного поля — ни частного, ни публичного."""
    _seed_sensitive(seeded_client)

    # заголовок ZBXD -- двоичный, декодируем только тело
    body = _packet_bytes(ZabbixConfig(server="10.0.0.5"))[protocol.HEADER_LEN :]

    found = re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", body.decode("utf-8"))
    assert not found, f"в пакете найдены адреса: {found}"


def test_the_query_itself_selects_no_sensitive_column(seeded_client):
    """Первый рубеж — сам SQL: чего он не выбирает, то и не может уехать."""
    _seed_sensitive(seeded_client)

    rows = db.get_fleet_verdicts()

    forbidden = {
        "owner_full_name",
        "owner_position",
        "owner_phone",
        "owner_full_name_operator",
        "comment",
        "comment_operator",
        "site_name",
        "local_ip",
        "serial_hash",
    }
    assert forbidden.isdisjoint(set(rows[0]))


def test_key_set_is_pinned_so_a_new_db_field_cannot_leak_by_itself(seeded_client):
    _seed_sensitive(seeded_client)
    rows = db.get_fleet_verdicts()

    packet = it.build_packet(rows, ZabbixConfig(server="10.0.0.5"), now=datetime.now(timezone.utc))

    assert {i.key for i in packet.items} <= it.declared_keys()


def test_device_id_naming_does_not_leak_the_hostname(seeded_client):
    """host_field=device_id — режим для тех, кто не хочет отдавать имена машин."""
    _seed_sensitive(seeded_client)
    hostname = db.get_device(HEALTHY_DEVICE)["hostname"]

    raw = _packet_bytes(ZabbixConfig(server="10.0.0.5", host_field="device_id"))

    assert hostname.encode("utf-8") not in raw
