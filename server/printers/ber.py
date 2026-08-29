"""Минимальный BER/ASN.1-кодек под SNMP v1/v2c (только нужные типы).

Кодирует/декодирует TLV, длину (короткая/длинная форма), INTEGER, OCTET STRING,
NULL и OID. Парсер работает с недоверенными данными из сети — все decode-функции
обязаны быть устойчивы к мусору (см. snmp.parse_response для границ).
"""

from typing import List, Tuple

# Потолок длины тела OID (стандартные OID < 50 байт): защита от CPU-амплификации
# на враждебном all-continuation OID (decode_oid → один bigint, str() O(n^2)).
_MAX_OID_BYTES = 128


def encode_length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body: List[int] = []
    while n > 0:
        body.insert(0, n & 0xFF)
        n >>= 8
    return bytes([0x80 | len(body)]) + bytes(body)


def decode_length(data: bytes, pos: int) -> Tuple[int, int]:
    """Вернуть (длина, число_съеденных_байт) начиная с pos."""
    first = data[pos]
    if first < 0x80:
        return first, 1
    num = first & 0x7F
    if pos + 1 + num > len(data):
        raise ValueError("truncated BER length")
    value = int.from_bytes(data[pos + 1 : pos + 1 + num], "big")
    return value, 1 + num


def encode_tlv(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + encode_length(len(body)) + body


def encode_integer(value: int) -> bytes:
    if value == 0:
        return encode_tlv(0x02, b"\x00")
    body: List[int] = []
    v = value
    while v not in (0, -1):
        body.insert(0, v & 0xFF)
        v >>= 8
    # Append final byte to complete the representation.
    # For positive values: we stopped at v=0, so append 0x00 if high bit is set.
    # For negative values: we stopped at v=-1, so append 0xFF to represent the sign.
    if value > 0:
        if body and body[0] & 0x80:
            body.insert(0, 0x00)
    else:
        # Negative value: append sign-extending 0xFF.
        # Ensure at least 1 byte of content (can't have empty body for INTEGER).
        if body and not (body[0] & 0x80):
            body.insert(0, 0xFF)
        elif not body:
            body.append(0xFF)
    return encode_tlv(0x02, bytes(body))


def encode_octet_string(value: bytes) -> bytes:
    return encode_tlv(0x04, value)


def encode_null() -> bytes:
    return encode_tlv(0x05, b"")


def encode_oid(oid: str) -> bytes:
    arcs = [int(a) for a in oid.split(".")]
    body = [40 * arcs[0] + arcs[1]]
    for arc in arcs[2:]:
        if arc < 0x80:
            body.append(arc)
            continue
        chunk = [arc & 0x7F]
        arc >>= 7
        while arc > 0:
            chunk.insert(0, (arc & 0x7F) | 0x80)
            arc >>= 7
        body.extend(chunk)
    return encode_tlv(0x06, bytes(body))


def decode_oid(body: bytes) -> str:
    if not body or len(body) > _MAX_OID_BYTES:
        return ""  # пустой/враждебно-длинный OID → "" (вызывающий отбросит варбайнд)
    first = body[0]
    arcs = [first // 40, first % 40]
    value = 0
    for b in body[1:]:
        value = (value << 7) | (b & 0x7F)
        if not b & 0x80:
            arcs.append(value)
            value = 0
    return ".".join(str(a) for a in arcs)


def decode_tlv(data: bytes, pos: int) -> Tuple[int, bytes, int]:
    """Вернуть (tag, value_bytes, next_pos)."""
    tag = data[pos]
    length, lsize = decode_length(data, pos + 1)
    start = pos + 1 + lsize
    if start + length > len(data):
        # Заявленная длина больше фактического остатка буфера -- Python-слайс
        # молча обрезал бы value до укороченного "мусора" вместо отбраковки
        # (P0-8). Тот же UNKNOWN-путь, что и decode_length: caller (snmp.py)
        # уже перехватывает ошибки парсинга недоверенного сетевого ввода.
        raise ValueError("truncated BER TLV")
    return tag, data[start : start + length], start + length


def decode_sequence(body: bytes) -> List[Tuple[int, bytes]]:
    items: List[Tuple[int, bytes]] = []
    pos = 0
    while pos < len(body):
        tag, value, pos = decode_tlv(body, pos)
        items.append((tag, value))
    return items
