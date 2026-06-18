"""Phase 1 — BER/ASN.1-кодек под SNMP v1/v2c: длина, TLV, примитивы, OID."""

import pytest
from server.printers import ber


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
