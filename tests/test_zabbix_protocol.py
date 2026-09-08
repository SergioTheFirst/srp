"""Протокол траппера Zabbix: сборка пакета и разбор ответа — чистые функции.

Эталон формата снят с настоящего zabbix_sender 7.0.30 (см. tests/test_zabbix_wire.py):
    ZBXD | flags(1) | length(LE uint32) | reserved(LE uint32) | JSON
    {"request":"sender data","data":[{"host":..,"key":..,"value":..}],"clock":..,"ns":..}
Ни один тест здесь не открывает сокет.
"""

from __future__ import annotations

import json
import struct
import zlib

import pytest
from server.zabbix import protocol as p


def _items(n: int) -> list:
    return [p.Item(host=f"host{i}", key="srp.state", value="h0") for i in range(n)]


# --------------------------------------------------------------------------- #
# Сборка пакета
# --------------------------------------------------------------------------- #
def test_packet_header_is_exactly_13_bytes_with_little_endian_length():
    raw = p.build_packet(_items(2), clock=1788864254, ns=107072800)

    assert raw[:4] == b"ZBXD"
    assert raw[4] == p.FLAG_ZABBIX
    length, reserved = struct.unpack("<II", raw[5:13])
    assert length == len(raw) - p.HEADER_LEN
    assert reserved == 0


def test_packet_body_matches_the_sender_data_shape():
    raw = p.build_packet([p.Item(host="BUH-01", key="srp.damage", value="62.5")], clock=17, ns=5)
    body = json.loads(raw[p.HEADER_LEN :])

    assert body["request"] == "sender data"
    assert body["clock"] == 17
    assert body["ns"] == 5
    assert body["data"] == [{"host": "BUH-01", "key": "srp.damage", "value": "62.5"}]


def test_packet_is_utf8_and_survives_cyrillic_values():
    raw = p.build_packet(
        [p.Item(host="BUH-01", key="srp.reason", value="Накопитель: заменить диск")],
        clock=1,
        ns=1,
    )
    body = json.loads(raw[p.HEADER_LEN :].decode("utf-8"))

    assert body["data"][0]["value"] == "Накопитель: заменить диск"
    # длина в заголовке считается в БАЙТАХ, а не в символах
    length = struct.unpack("<I", raw[5:9])[0]
    assert length == len(raw) - p.HEADER_LEN


def test_empty_item_list_builds_nothing():
    assert p.build_packet([], clock=1, ns=1) is None


# --------------------------------------------------------------------------- #
# Разбиение на пачки
# --------------------------------------------------------------------------- #
def test_chunks_match_the_real_sender_batch_size_of_250():
    # снято с живого zabbix_sender 7.0.30: 1200 значений -> 250,250,250,250,200
    sizes = [len(c) for c in p.chunks(_items(1200))]
    assert sizes == [250, 250, 250, 250, 200]


def test_chunks_of_a_short_list_is_one_batch():
    assert [len(c) for c in p.chunks(_items(7))] == [7]


def test_chunks_of_nothing_is_nothing():
    assert list(p.chunks([])) == []


# --------------------------------------------------------------------------- #
# Разбор ответа
# --------------------------------------------------------------------------- #
def _frame(obj, flags=p.FLAG_ZABBIX) -> bytes:
    payload = json.dumps(obj).encode("utf-8")
    return b"ZBXD" + bytes([flags]) + struct.pack("<II", len(payload), 0) + payload


def test_successful_response_is_parsed_into_counts():
    raw = _frame(
        {
            "response": "success",
            "info": "processed: 42; failed: 14; total: 56; seconds spent: 0.000123",
        }
    )
    r = p.parse_response(raw)

    assert r.ok is True
    assert (r.processed, r.failed, r.total) == (42, 14, 56)


def test_response_without_info_counts_is_ok_but_counts_are_unknown():
    r = p.parse_response(_frame({"response": "success"}))

    assert r.ok is True
    assert r.processed is None and r.failed is None


def test_failed_response_is_not_ok_and_keeps_the_info_text():
    r = p.parse_response(_frame({"response": "failed", "info": "host [h1] not found"}))

    assert r.ok is False
    assert "not found" in r.info


def test_non_zbxd_answer_is_rejected():
    with pytest.raises(p.ProtocolError):
        p.parse_response(b"HTTP/1.1 200 OK\r\n\r\n<html>")


def test_truncated_header_is_rejected():
    with pytest.raises(p.ProtocolError):
        p.parse_response(b"ZBXD\x01\x05")


def test_body_shorter_than_declared_length_is_rejected():
    raw = b"ZBXD\x01" + struct.pack("<II", 100, 0) + b"{}"
    with pytest.raises(p.ProtocolError):
        p.parse_response(raw)


def test_declared_length_above_the_ceiling_is_rejected_before_reading():
    with pytest.raises(p.ProtocolError):
        p.check_declared_length(p.MAX_RESPONSE_BYTES + 1)


def test_non_json_body_is_rejected():
    payload = b"not json at all"
    raw = b"ZBXD\x01" + struct.pack("<II", len(payload), 0) + payload
    with pytest.raises(p.ProtocolError):
        p.parse_response(raw)


def test_compressed_response_is_decompressed():
    payload = zlib.compress(json.dumps({"response": "success", "info": "processed: 1"}).encode())
    raw = (
        b"ZBXD"
        + bytes([p.FLAG_ZABBIX | p.FLAG_COMPRESSED])
        + struct.pack("<II", len(payload), 999)
        + payload
    )
    r = p.parse_response(raw)

    assert r.ok is True
    assert r.processed == 1


def test_broken_compressed_body_is_rejected_not_crashed():
    payload = b"\x78\x9c" + b"garbage"
    raw = (
        b"ZBXD"
        + bytes([p.FLAG_ZABBIX | p.FLAG_COMPRESSED])
        + struct.pack("<II", len(payload), 10)
        + payload
    )
    with pytest.raises(p.ProtocolError):
        p.parse_response(raw)


# --------------------------------------------------------------------------- #
# Форматирование значений
# --------------------------------------------------------------------------- #
def test_numbers_are_rendered_without_scientific_notation_or_trailing_noise():
    assert p.fmt_value(62.5) == "62.5"
    assert p.fmt_value(62.0) == "62"
    assert p.fmt_value(0.000001) == "0"
    assert p.fmt_value(3) == "3"
    assert p.fmt_value(True) == "1"


def test_text_values_are_capped_so_a_long_reason_cannot_bloat_the_packet():
    long_text = "я" * 1000
    out = p.fmt_value(long_text)

    assert len(out) <= p.MAX_VALUE_CHARS
    assert out.startswith("я")


# --------------------------------------------------------------------------- #
# Враждебный ответ (находки security-ревью)
# --------------------------------------------------------------------------- #
def test_zip_bomb_is_rejected_instead_of_allocating_a_gigabyte():
    """~1 МБ на проводе разворачивался в 1 ГиБ: потолок проверялся до распаковки."""
    payload = zlib.compress(b"0" * (4 * p.MAX_RESPONSE_BYTES))
    assert len(payload) < p.MAX_RESPONSE_BYTES, "фикстура должна пролезать через потолок"
    raw = (
        b"ZBXD"
        + bytes([p.FLAG_ZABBIX | p.FLAG_COMPRESSED])
        + struct.pack("<II", len(payload), 0)
        + payload
    )

    with pytest.raises(p.ProtocolError):
        p.parse_response(raw)


def test_enormous_number_in_info_does_not_raise():
    """int() на 50 000 цифр бросает ValueError на 3.11+ и квадратичен на 3.9."""
    raw = _frame({"response": "success", "info": "processed: " + "9" * 50000})

    r = p.parse_response(raw)

    assert r.ok is True
    assert r.processed is None, "неправдоподобное число — это отсутствие числа"


def test_deeply_nested_json_is_a_protocol_error_not_a_recursion_error():
    payload = (b"[" * 5000) + (b"]" * 5000)
    raw = b"ZBXD\x01" + struct.pack("<II", len(payload), 0) + payload

    with pytest.raises(p.ProtocolError):
        p.parse_response(raw)


def test_info_text_is_capped_so_it_cannot_live_in_memory_forever():
    raw = _frame({"response": "failed", "info": "A" * 100000})

    r = p.parse_response(raw)

    assert len(r.info) <= p.MAX_INFO_CHARS


def test_lone_surrogate_in_info_cannot_poison_the_status_page():
    """Один такой ответ ронял /pipeline и /api/v1/zabbix/status до перезапуска."""
    payload = rb'{"response":"failed","info":"\ud800bad"}'
    raw = b"ZBXD\x01" + struct.pack("<II", len(payload), 0) + payload

    r = p.parse_response(raw)

    r.info.encode("utf-8")  # не должно бросить
    assert "bad" in r.info


def test_newline_in_info_cannot_forge_a_log_line():
    raw = _frame({"response": "failed", "info": "processed: 0\n2026-09-08 INFO поддельная"})

    r = p.parse_response(raw)

    assert "\n" not in r.info
