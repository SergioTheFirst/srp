"""Phase 1 — SNMP v1/v2c: сборка запроса, разбор ответа, транспорт, walk."""

import os
import socket
import threading

from server.printers import ber, snmp


def test_build_get_is_decodable():
    pkt = snmp.build_request(
        snmp.GET, ["1.3.6.1.2.1.1.5.0"], community="public", version=1, request_id=42
    )
    tag, msg_body, _ = ber.decode_tlv(pkt, 0)
    assert tag == 0x30  # SEQUENCE
    items = ber.decode_sequence(msg_body)
    assert items[0] == (0x02, b"\x01")  # version v2c
    assert items[1] == (0x04, b"public")  # community
    assert items[2][0] == snmp.GET  # PDU тег 0xA0


def _build_response(varbinds: bytes, *, request_id: int = 7) -> bytes:
    pdu_body = (
        ber.encode_integer(request_id)
        + ber.encode_integer(0)
        + ber.encode_integer(0)
        + ber.encode_tlv(0x30, varbinds)
    )
    pdu = ber.encode_tlv(snmp.GETRESPONSE, pdu_body)
    return ber.encode_tlv(0x30, ber.encode_integer(1) + ber.encode_octet_string(b"public") + pdu)


def test_parse_response_extracts_varbinds():
    name = "1.3.6.1.2.1.1.5.0"
    marker = "1.3.6.1.2.1.43.10.2.1.4.1.1"
    vb1 = ber.encode_tlv(0x30, ber.encode_oid(name) + ber.encode_octet_string(b"PRN-1"))
    vb2 = ber.encode_tlv(
        0x30, ber.encode_oid(marker) + ber.encode_tlv(0x41, b"\x27\x10")
    )  # Counter32 = 10000
    parsed = snmp.parse_response(_build_response(vb1 + vb2))
    assert parsed[name] == "PRN-1"
    assert parsed[marker] == 10000


def test_parse_response_decodes_value_types():
    vbs = b"".join(
        [
            ber.encode_tlv(
                0x30, ber.encode_oid("1.3.6.1.2.1.1.7.0") + ber.encode_integer(-5)
            ),  # INTEGER signed
            ber.encode_tlv(
                0x30,
                ber.encode_oid("1.3.6.1.2.1.1.2.0") + ber.encode_oid("1.3.6.1.4.1.11"),
            ),  # OID value
            ber.encode_tlv(
                0x30,
                ber.encode_oid("1.3.6.1.2.1.4.20.1.1")
                + ber.encode_tlv(0x40, bytes([192, 168, 1, 9])),
            ),  # IpAddress
            ber.encode_tlv(
                0x30,
                ber.encode_oid("1.3.6.1.2.1.43.10.2.1.4.1.1") + ber.encode_tlv(0x46, b"\x01\x00"),
            ),  # Counter64 = 256
        ]
    )
    parsed = snmp.parse_response(_build_response(vbs))
    assert parsed["1.3.6.1.2.1.1.7.0"] == -5
    assert parsed["1.3.6.1.2.1.1.2.0"] == "1.3.6.1.4.1.11"
    assert parsed["1.3.6.1.2.1.4.20.1.1"] == "192.168.1.9"
    assert parsed["1.3.6.1.2.1.43.10.2.1.4.1.1"] == 256


def test_build_getbulk_carries_nonrepeaters_and_maxreps():
    pkt = snmp.build_request(
        snmp.GETBULK,
        ["1.3.6.1.2.1.43.11.1.1.6"],
        community="public",
        version=1,
        request_id=5,
        non_repeaters=0,
        max_repetitions=10,
    )
    _tag, msg_body, _ = ber.decode_tlv(pkt, 0)
    items = ber.decode_sequence(msg_body)
    assert items[2][0] == snmp.GETBULK
    pdu_items = ber.decode_sequence(items[2][1])  # rid, non_repeaters, max_reps, vbinds
    assert pdu_items[0] == (0x02, b"\x05")
    assert pdu_items[2] == (0x02, b"\x0a")  # max_repetitions = 10


def test_parse_response_exception_and_null_become_none():
    oid_a = "1.3.6.1.2.1.1.1.0"
    oid_b = "1.3.6.1.2.1.1.6.0"
    vb1 = ber.encode_tlv(0x30, ber.encode_oid(oid_a) + b"\x80\x00")  # noSuchObject
    vb2 = ber.encode_tlv(0x30, ber.encode_oid(oid_b) + ber.encode_null())
    parsed = snmp.parse_response(_build_response(vb1 + vb2))
    assert parsed[oid_a] is None
    assert parsed[oid_b] is None


def test_parse_response_garbage_returns_empty():
    assert snmp.parse_response(b"\x00\x01\x02garbage") == {}
    assert snmp.parse_response(b"") == {}


def _request_id_of(pkt: bytes) -> int:
    """Извлечь request-id из запроса — стаб эхает его, как реальный агент."""
    _t, msg, _ = ber.decode_tlv(pkt, 0)
    items = ber.decode_sequence(msg)
    pdu_items = ber.decode_sequence(items[2][1])
    return int.from_bytes(pdu_items[0][1], "big", signed=True)


def _udp_echo_server(varbind: bytes) -> socket.socket:
    """Однократно отвечает GetResponse с варбайндом, эхом request-id запроса."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))

    def serve() -> None:
        try:
            data, addr = srv.recvfrom(65535)
            srv.sendto(_build_response(varbind, request_id=_request_id_of(data)), addr)
        except OSError:
            pass

    threading.Thread(target=serve, daemon=True).start()
    return srv


def _udp_fixed_server(reply: bytes) -> socket.socket:
    """Однократно отвечает фиксированным пакетом (игнорируя запрос)."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))

    def serve() -> None:
        try:
            _data, addr = srv.recvfrom(65535)
            srv.sendto(reply, addr)
        except OSError:
            pass

    threading.Thread(target=serve, daemon=True).start()
    return srv


def test_snmp_get_round_trip_over_udp():
    name = "1.3.6.1.2.1.1.5.0"
    vb = ber.encode_tlv(0x30, ber.encode_oid(name) + ber.encode_octet_string(b"PRN-9"))
    srv = _udp_echo_server(vb)
    try:
        port = srv.getsockname()[1]
        result = snmp.snmp_get("127.0.0.1", [name], port=port, timeout=2.0)
        assert result[name] == "PRN-9"
    finally:
        srv.close()


def test_snmp_get_timeout_returns_empty():
    # Закрытый порт без слушателя → таймаут → пустой dict, не зависание.
    result = snmp.snmp_get("127.0.0.1", ["1.3.6.1.2.1.1.5.0"], port=1, timeout=0.2)
    assert result == {}


def test_parse_response_without_pdu_returns_empty():
    # Валидный внешний SEQUENCE, но внутри только version+community, без PDU.
    msg = ber.encode_tlv(0x30, ber.encode_integer(1) + ber.encode_octet_string(b"public"))
    assert snmp.parse_response(msg) == {}


def test_snmp_walk_dead_host_returns_empty():
    # Нет слушателя → транзакция пустая на первой итерации → walk не виснет.
    result = snmp.snmp_walk("127.0.0.1", "1.3.6.1.2.1.43.11.1.1.6", port=1, timeout=0.2)
    assert result == {}


def _udp_walk_server(replies: list) -> socket.socket:
    """Отвечает по одному varbind на запрос (по списку), затем замолкает."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    srv.bind(("127.0.0.1", 0))
    state = {"i": 0}

    def serve() -> None:
        while True:
            try:
                data, addr = srv.recvfrom(65535)
            except OSError:
                return
            i = state["i"]
            if i >= len(replies):
                return
            state["i"] += 1
            oid_str, valtlv = replies[i]
            vb = ber.encode_tlv(0x30, ber.encode_oid(oid_str) + valtlv)
            srv.sendto(_build_response(vb, request_id=_request_id_of(data)), addr)

    threading.Thread(target=serve, daemon=True).start()
    return srv


def test_snmp_walk_collects_subtree_and_stops_at_prefix_exit():
    base = "1.3.6.1.2.1.43.11.1.1.6"  # supply_desc
    replies = [
        (base + ".1.1", ber.encode_octet_string("Чёрный тонер".encode())),
        (base + ".1.2", ber.encode_octet_string(b"Cyan")),
        ("1.3.6.1.2.1.43.11.1.1.7.1.1", ber.encode_octet_string(b"unit")),  # вне поддерева
    ]
    srv = _udp_walk_server(replies)
    try:
        port = srv.getsockname()[1]
        result = snmp.snmp_walk("127.0.0.1", base, port=port, timeout=2.0)
        assert result == {base + ".1.1": "Чёрный тонер", base + ".1.2": "Cyan"}
    finally:
        srv.close()


def test_snmp_walk_stops_on_no_progress():
    base = "1.3.6.1.2.1.43.11.1.1.6"
    same = (base + ".1.1", ber.encode_octet_string(b"X"))
    srv = _udp_walk_server([same] * 50)  # всегда один и тот же OID → нет прогресса
    try:
        port = srv.getsockname()[1]
        result = snmp.snmp_walk("127.0.0.1", base, port=port, timeout=2.0, max_rows=512)
        assert result == {base + ".1.1": "X"}  # не зациклился
    finally:
        srv.close()


def test_snmp_get_rejects_mismatched_request_id():
    # Ответ с чужим request_id (подделка/устаревший) → отброшен → {} (HIGH-1).
    name = "1.3.6.1.2.1.1.5.0"
    vb = ber.encode_tlv(0x30, ber.encode_oid(name) + ber.encode_octet_string(b"FAKE"))
    srv = _udp_fixed_server(_build_response(vb, request_id=999999))
    try:
        port = srv.getsockname()[1]
        result = snmp.snmp_get("127.0.0.1", [name], port=port, timeout=0.5, retries=0)
        assert result == {}
    finally:
        srv.close()


def test_parse_response_fuzz_never_raises():
    # Внешняя сетевая граница: любой случайный мусор → dict, без исключений/зависаний.
    for n in range(0, 80):
        assert isinstance(snmp.parse_response(os.urandom(n)), dict)
