"""SNMP v1/v2c поверх stdlib (socket + ber). Только ЧТЕНИЕ: GET/GETNEXT/GETBULK.

Никогда не шлёт SET — движок наблюдения, не управления. Разбор ответа работает с
недоверенными UDP-данными: любой мусор/обрыв → пустой результат, без исключений
наружу и без зависаний (жёсткий таймаут на сокете).
"""

import socket
from typing import Dict, List, Set, Tuple

from server.printers import ber

GET = 0xA0
GETNEXT = 0xA1
GETRESPONSE = 0xA2
GETBULK = 0xA5
_SEQUENCE = 0x30


def build_request(
    pdu_type: int,
    oids: List[str],
    *,
    community: str,
    version: int,
    request_id: int,
    non_repeaters: int = 0,
    max_repetitions: int = 0,
) -> bytes:
    varbinds = b"".join(
        ber.encode_tlv(_SEQUENCE, ber.encode_oid(o) + ber.encode_null()) for o in oids
    )
    vb_seq = ber.encode_tlv(_SEQUENCE, varbinds)
    if pdu_type == GETBULK:
        pdu_body = (
            ber.encode_integer(request_id)
            + ber.encode_integer(non_repeaters)
            + ber.encode_integer(max_repetitions)
            + vb_seq
        )
    else:
        pdu_body = (
            ber.encode_integer(request_id) + ber.encode_integer(0) + ber.encode_integer(0) + vb_seq
        )
    pdu = ber.encode_tlv(pdu_type, pdu_body)
    msg = ber.encode_integer(version) + ber.encode_octet_string(community.encode()) + pdu
    return ber.encode_tlv(_SEQUENCE, msg)


def _decode_value(tag: int, body: bytes) -> object:
    if tag == 0x02:  # INTEGER (signed)
        return int.from_bytes(body, "big", signed=True) if body else 0
    if tag in (0x41, 0x42, 0x43, 0x46):  # Counter32 / Gauge32 / TimeTicks / Counter64
        return int.from_bytes(body, "big", signed=False) if body else 0
    if tag == 0x04:  # OCTET STRING — best-effort utf-8, иначе latin-1 (без потерь)
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            return body.decode("latin-1")
    if tag == 0x06:  # OID
        return ber.decode_oid(body) if body else ""
    if tag == 0x40:  # IpAddress
        return ".".join(str(b) for b in body)
    # 0x05 NULL и исключения noSuchObject/noSuchInstance/endOfMibView → None.
    return None


def _find_first(items: List[Tuple[int, bytes]], wanted: Set[int]) -> bytes:
    for tag, value in items:
        if tag in wanted:
            return value
    return b""


def parse_response(data: bytes) -> Dict[str, object]:
    """Разобрать SNMP-сообщение в {oid: значение}. Мусор → {} (без исключений)."""
    result: Dict[str, object] = {}
    try:
        tag, msg_body, _ = ber.decode_tlv(data, 0)
        if tag != _SEQUENCE:
            return result
        pdu_body = _find_first(ber.decode_sequence(msg_body), {GET, GETNEXT, GETRESPONSE, GETBULK})
        if not pdu_body:
            return result
        varbinds_seq = _find_first(ber.decode_sequence(pdu_body), {_SEQUENCE})
        if not varbinds_seq:
            return result
        for vbtag, vbval in ber.decode_sequence(varbinds_seq):
            if vbtag != _SEQUENCE:
                continue
            fields = ber.decode_sequence(vbval)
            if len(fields) < 2 or fields[0][0] != 0x06:
                continue
            oid = ber.decode_oid(fields[0][1])
            result[oid] = _decode_value(fields[1][0], fields[1][1])
    except (IndexError, ValueError):
        return {}
    return result


def _transact(host: str, port: int, pkt: bytes, timeout: float, retries: int) -> Dict[str, object]:
    """Отправить один пакет, вернуть разобранный ответ. Любой сбой → {}, не виснет."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        for _ in range(retries + 1):
            try:
                sock.sendto(pkt, (host, port))
                data, _addr = sock.recvfrom(65535)
                return parse_response(data)
            except OSError:
                # timeout / ICMP port-unreachable (WinError 10054) / битый хост —
                # любой транспортный сбой = «нет ответа» → UNKNOWN, не краш.
                continue
        return {}
    finally:
        sock.close()


def snmp_get(
    host: str,
    oids: List[str],
    *,
    community: str = "public",
    version: int = 1,
    port: int = 161,
    timeout: float = 1.0,
    retries: int = 1,
    request_id: int = 1,
) -> Dict[str, object]:
    """GET к host:port. Нет ответа в срок → {} (не виснет). Только чтение."""
    pkt = build_request(GET, oids, community=community, version=version, request_id=request_id)
    return _transact(host, port, pkt, timeout, retries)


def snmp_walk(
    host: str,
    base_oid: str,
    *,
    community: str = "public",
    version: int = 1,
    port: int = 161,
    timeout: float = 1.0,
    retries: int = 1,
    max_rows: int = 512,
) -> Dict[str, object]:
    """Обойти табличную базу OID через GETNEXT. Ключ = полный OID.

    Останавливается при выходе за префикс base_oid, отсутствии прогресса
    (защита от зацикливания на битом агенте) или лимите max_rows. GETBULK (v2c)
    как оптимизацию не используем: таблицы принтеров малы, GETNEXT надёжнее на
    недоверенном вводе. Только чтение.
    """
    result: Dict[str, object] = {}
    current = base_oid
    prefix = base_oid + "."
    for rid in range(1, max_rows + 1):
        pkt = build_request(
            GETNEXT, [current], community=community, version=version, request_id=rid
        )
        parsed = _transact(host, port, pkt, timeout, retries)
        if not parsed:
            break
        next_current = None
        left_subtree = False
        for oid, value in parsed.items():
            if oid == base_oid or oid.startswith(prefix):
                if oid in result:  # уже видели → прогресса нет
                    continue
                result[oid] = value
                if next_current is None or oid > next_current:
                    next_current = oid
            else:
                left_subtree = True
        if left_subtree or next_current is None or next_current == current:
            break
        current = next_current
    return result
