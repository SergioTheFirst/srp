"""Phase 1 — BER/ASN.1-кодек под SNMP v1/v2c: длина, TLV, примитивы, OID."""

import socket
import threading

import pytest
from server.printers import ber, snmp


def test_length_short_form():
    assert ber.encode_length(5) == b"\x05"
    assert ber.decode_length(b"\x05rest", 0) == (5, 1)


def test_length_long_form():
    assert ber.encode_length(200) == b"\x81\xc8"
    assert ber.encode_length(300) == b"\x82\x01\x2c"
    assert ber.decode_length(b"\x82\x01\x2c", 0) == (300, 3)


def test_encode_integer_and_octet_string():
    assert ber.encode_integer(0) == b"\x02\x01\x00"
    assert ber.encode_integer(127) == b"\x02\x01\x7f"
    assert ber.encode_octet_string(b"public") == b"\x04\x06public"
    assert ber.encode_null() == b"\x05\x00"


def test_encode_integer_multibyte_and_high_bit():
    # 128 нуждается в ведущем 0x00, чтобы остаться положительным.
    assert ber.encode_integer(128) == b"\x02\x02\x00\x80"
    assert ber.encode_integer(256) == b"\x02\x02\x01\x00"


def test_encode_integer_negative_one():
    # -1 должен кодироваться как 0xFF (все биты установлены).
    # Тело должно иметь минимум 1 октет (валидный BER INTEGER).
    encoded = ber.encode_integer(-1)
    assert encoded == b"\x02\x01\xff"  # tag=0x02, length=1, body=0xff


def test_encode_integer_negative_values_roundtrip():
    # Регрессионный тест: убедиться, что существующие положительные значения
    # не изменились после добавления поддержки отрицательных чисел.
    assert ber.encode_integer(0) == b"\x02\x01\x00"
    assert ber.encode_integer(127) == b"\x02\x01\x7f"
    assert ber.encode_integer(128) == b"\x02\x02\x00\x80"
    assert ber.encode_integer(256) == b"\x02\x02\x01\x00"


def test_encode_decode_oid_roundtrip():
    oid = "1.3.6.1.2.1.1.5.0"
    enc = ber.encode_oid(oid)
    assert enc[0] == 0x06
    assert ber.decode_oid(enc[2:]) == oid


def test_encode_oid_handles_large_arc():
    # Enterprise-арк 18334 (Konica Minolta) > 127 → многобайтовая база-128.
    oid = "1.3.6.1.4.1.18334.1"
    enc = ber.encode_oid(oid)
    assert ber.decode_oid(enc[2:]) == oid


def test_decode_tlv_returns_tag_value_next():
    tag, value, nxt = ber.decode_tlv(b"\x02\x01\x2a", 0)
    assert tag == 0x02 and value == b"\x2a" and nxt == 3


def test_decode_sequence_splits_items():
    body = ber.encode_integer(1) + ber.encode_octet_string(b"x")
    items = ber.decode_sequence(body)
    assert items == [(0x02, b"\x01"), (0x04, b"x")]


def test_decode_oid_rejects_oversized_and_empty_body():
    assert ber.decode_oid(b"") == ""
    assert ber.decode_oid(b"\xff" * 200) == ""  # враждебно-длинный → "" (не O(n^2))


def test_decode_length_raises_on_truncated_long_form():
    with pytest.raises(ValueError):
        ber.decode_length(b"\x82\x01", 0)  # объявлено 2 байта длины, присутствует 1


def test_decode_tlv_raises_on_length_exceeding_remaining_buffer():
    # Заявлено 10 байт значения, но после заголовка TLV осталось только 3 (P0-8).
    with pytest.raises(ValueError):
        ber.decode_tlv(b"\x04\x0aabc", 0)


def test_decode_tlv_accepts_length_exactly_matching_buffer_end():
    # Граница: заявленная длина == фактический остаток -- НЕ должно бросать.
    tag, value, nxt = ber.decode_tlv(b"\x04\x03abc", 0)
    assert tag == 0x04 and value == b"abc" and nxt == 5


# --- B7: snmp_walk() выбор GETBULK/GETNEXT (server/printers/snmp.py) --------


def _pdu_tag_of(pkt: bytes) -> int:
    """Тег PDU (GETBULK/GETNEXT/...) из запроса snmp_walk, для проверки со стороны фейкового агента."""
    _tag, msg_body, _ = ber.decode_tlv(pkt, 0)
    items = ber.decode_sequence(msg_body)
    return items[2][0]


def _request_id_of(pkt: bytes) -> int:
    """request-id запроса — стаб эхает его в ответе, как реальный агент."""
    _t, msg, _ = ber.decode_tlv(pkt, 0)
    items = ber.decode_sequence(msg)
    pdu_items = ber.decode_sequence(items[2][1])
    return int.from_bytes(pdu_items[0][1], "big", signed=True)


def _build_walk_response(varbinds: bytes, *, request_id: int) -> bytes:
    pdu_body = (
        ber.encode_integer(request_id)
        + ber.encode_integer(0)
        + ber.encode_integer(0)
        + ber.encode_tlv(0x30, varbinds)
    )
    pdu = ber.encode_tlv(snmp.GETRESPONSE, pdu_body)
    return ber.encode_tlv(0x30, ber.encode_integer(1) + ber.encode_octet_string(b"public") + pdu)


def _numeric_oid_key(o: str) -> tuple:
    return tuple(int(p) for p in o.split("."))


def _requested_oid_and_max_reps(pkt: bytes) -> tuple:
    """(запрошенный OID, max_repetitions) из GETBULK-запроса — честный фейковый
    агент продолжает СТРОГО отсюда в числовом порядке (как настоящий MIB-обход),
    а не по собственному счётчику (иначе тест не поймает лексикографическую
    регрессию курсора, F3)."""
    _tag, msg_body, _ = ber.decode_tlv(pkt, 0)
    items = ber.decode_sequence(msg_body)
    pdu_items = ber.decode_sequence(items[2][1])
    max_repetitions = int.from_bytes(pdu_items[2][1], "big", signed=True)
    varbinds = ber.decode_sequence(pdu_items[3][1])
    first_vb_fields = ber.decode_sequence(varbinds[0][1])
    oid = ber.decode_oid(first_vb_fields[0][1])
    return oid, max_repetitions


def test_v2c_walk_uses_getbulk_and_batches_rows():
    # Фейковый агент отвечает СТРОГО по запрошенному OID в числовом порядке (не по
    # собственному счётчику) — таблица нарочно пересекает границы разрядов
    # (…2.9 → …2.10 → …2.11), чтобы поймать лексикографическую регрессию курсора (F3):
    # со строковым сравнением next_current после первой пачки (1..25) откатывается
    # на «9» вместо «25», и обход делает лишний перекрывающийся запрос.
    base = "1.3.6.1.2.1.2.2.1.2"
    total_rows = 100
    oids = sorted((f"{base}.{i}" for i in range(1, total_rows + 1)), key=_numeric_oid_key)
    tags: list = []
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))

    def serve() -> None:
        while True:
            try:
                data, addr = srv.recvfrom(65535)
            except OSError:
                return
            tags.append(_pdu_tag_of(data))
            requested, max_reps = _requested_oid_and_max_reps(data)
            requested_key = _numeric_oid_key(requested)
            batch = [o for o in oids if _numeric_oid_key(o) > requested_key][: max(max_reps, 1)]
            if not batch:
                return
            varbinds = b"".join(
                ber.encode_tlv(0x30, ber.encode_oid(o) + ber.encode_octet_string(b"eth"))
                for o in batch
            )
            srv.sendto(_build_walk_response(varbinds, request_id=_request_id_of(data)), addr)

    threading.Thread(target=serve, daemon=True).start()
    try:
        port = srv.getsockname()[1]
        result = snmp.snmp_walk(
            "127.0.0.1", base, version=1, port=port, timeout=2.0, max_rows=total_rows
        )
    finally:
        srv.close()

    assert len(result) == total_rows
    assert tags and all(tag == snmp.GETBULK for tag in tags)
    assert len(tags) == 4  # ceil(100/25) -- никаких перекрывающихся откатов курсора (F3)


def test_v1_walk_still_uses_getnext():
    base = "1.3.6.1.2.1.43.11.1.1.6"
    replies = [
        (base + ".1.1", ber.encode_octet_string(b"Black")),
        ("1.3.6.1.2.1.43.11.1.1.7.1.1", ber.encode_octet_string(b"unit")),  # вне поддерева
    ]
    tags: list = []
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    state = {"i": 0}

    def serve() -> None:
        while True:
            try:
                data, addr = srv.recvfrom(65535)
            except OSError:
                return
            tags.append(_pdu_tag_of(data))
            i = state["i"]
            if i >= len(replies):
                return
            state["i"] += 1
            oid_str, valtlv = replies[i]
            vb = ber.encode_tlv(0x30, ber.encode_oid(oid_str) + valtlv)
            srv.sendto(_build_walk_response(vb, request_id=_request_id_of(data)), addr)

    threading.Thread(target=serve, daemon=True).start()
    try:
        port = srv.getsockname()[1]
        result = snmp.snmp_walk("127.0.0.1", base, version=0, port=port, timeout=2.0)
    finally:
        srv.close()

    assert result == {base + ".1.1": "Black"}
    assert tags == [snmp.GETNEXT, snmp.GETNEXT]


def test_getbulk_empty_falls_back_to_getnext():
    # Первый ответ на GETBULK — пустой варбайнд-набор (агент его отверг) →
    # откат на GETNEXT для остатка обхода.
    base = "1.3.6.1.2.1.2.2.1.2"
    target = base + ".1"
    tags: list = []
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    state = {"n": 0}

    def serve() -> None:
        while True:
            try:
                data, addr = srv.recvfrom(65535)
            except OSError:
                return
            tags.append(_pdu_tag_of(data))
            state["n"] += 1
            if state["n"] == 1:
                reply = _build_walk_response(b"", request_id=_request_id_of(data))
            else:
                vb = ber.encode_tlv(0x30, ber.encode_oid(target) + ber.encode_octet_string(b"eth0"))
                reply = _build_walk_response(vb, request_id=_request_id_of(data))
            srv.sendto(reply, addr)

    threading.Thread(target=serve, daemon=True).start()
    try:
        port = srv.getsockname()[1]
        result = snmp.snmp_walk("127.0.0.1", base, version=1, port=port, timeout=2.0)
    finally:
        srv.close()

    assert result == {target: "eth0"}
    assert tags[0] == snmp.GETBULK
    assert snmp.GETNEXT in tags[1:]
