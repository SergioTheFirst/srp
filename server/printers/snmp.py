"""SNMP v1/v2c поверх stdlib (socket + ber). Только ЧТЕНИЕ: GET/GETNEXT/GETBULK.

Никогда не шлёт SET — движок наблюдения, не управления. Разбор ответа работает с
недоверенными UDP-данными: любой мусор/обрыв → пустой результат, без исключений
наружу и без зависаний (жёсткий таймаут на сокете).
"""

import secrets
import socket
from typing import Dict, List, Optional, Set, Tuple

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


def _parse_message(data: bytes) -> Tuple[Optional[int], Dict[str, object]]:
    """Разобрать SNMP-сообщение → (request_id, {oid: значение}).

    request_id = None, если PDU/поле не разобралось. Внешняя сетевая граница:
    ЛЮБОЙ сбой на недоверенном вводе → (None, {}), наружу не бросает.
    """
    result: Dict[str, object] = {}
    try:
        tag, msg_body, _ = ber.decode_tlv(data, 0)
        if tag != _SEQUENCE:
            return None, result
        pdu_body = _find_first(ber.decode_sequence(msg_body), {GET, GETNEXT, GETRESPONSE, GETBULK})
        if not pdu_body:
            return None, result
        pdu_items = ber.decode_sequence(pdu_body)
        request_id: Optional[int] = None
        if pdu_items and pdu_items[0][0] == 0x02:
            request_id = int.from_bytes(pdu_items[0][1], "big", signed=True)
        varbinds_seq = _find_first(pdu_items, {_SEQUENCE})
        if not varbinds_seq:
            return request_id, result
        for vbtag, vbval in ber.decode_sequence(varbinds_seq):
            if vbtag != _SEQUENCE:
                continue
            fields = ber.decode_sequence(vbval)
            if len(fields) < 2 or fields[0][0] != 0x06:
                continue
            oid = ber.decode_oid(fields[0][1])
            if not oid:  # пустой/враждебно-длинный OID → отбрасываем варбайнд
                continue
            result[oid] = _decode_value(fields[1][0], fields[1][1])
        return request_id, result
    except Exception:  # noqa: BLE001
        # Defense-in-depth: парсер недоверенной сети не должен ронять поллер ни на
        # каком вводе (security review LOW-4). Любой сбой → пусто = UNKNOWN.
        return None, {}


def parse_response(data: bytes) -> Dict[str, object]:
    """Разобрать SNMP-сообщение в {oid: значение}. Мусор → {} (без исключений)."""
    return _parse_message(data)[1]


def _transact(
    host: str, port: int, pkt: bytes, timeout: float, retries: int, request_id: int
) -> Dict[str, object]:
    """Отправить пакет, вернуть разобранный ответ. Любой сбой → {}, не виснет.

    Принимает ответ ТОЛЬКО от целевого IP и ТОЛЬКО с совпадающим request_id —
    отбрасывает подделанные/устаревшие UDP-датаграммы (security review HIGH-1).
    """
    try:
        target_ip = socket.gethostbyname(host)
    except OSError:
        return {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        for _ in range(retries + 1):
            try:
                sock.sendto(pkt, (target_ip, port))
                data, addr = sock.recvfrom(65535)
            except OSError:
                # timeout / ICMP port-unreachable (WinError 10054) / битый хост —
                # любой транспортный сбой = «нет ответа» → UNKNOWN, не краш.
                continue
            if addr[0] != target_ip:
                continue  # чужой источник — спуф/устаревшая датаграмма
            rid, parsed = _parse_message(data)
            if rid is not None and rid != request_id:
                continue  # не наш request-id — устаревший/подделка
            return parsed
        return {}
    finally:
        sock.close()


def _new_request_id() -> int:
    """Случайный 31-битный request-id: рассинхронизирует устаревшие/подделанные
    ответы (security review HIGH-1). secrets, не random → без шума bandit B311."""
    return secrets.randbelow(0x7FFFFFFF) + 1


def snmp_get(
    host: str,
    oids: List[str],
    *,
    community: str = "public",
    version: int = 1,
    port: int = 161,
    timeout: float = 1.0,
    retries: int = 1,
) -> Dict[str, object]:
    """GET к host:port. Нет ответа в срок → {} (не виснет). Только чтение."""
    request_id = _new_request_id()
    pkt = build_request(GET, oids, community=community, version=version, request_id=request_id)
    return _transact(host, port, pkt, timeout, retries, request_id)


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
    for _ in range(max_rows):
        request_id = _new_request_id()
        pkt = build_request(
            GETNEXT,
            [current],
            community=community,
            version=version,
            request_id=request_id,
        )
        parsed = _transact(host, port, pkt, timeout, retries, request_id)
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


class SnmpSession:
    """Привязанная к хосту SNMP-сессия для драйверов (.get/.walk). Только чтение."""

    def __init__(
        self,
        host: str,
        *,
        community: str = "public",
        version: int = 1,
        port: int = 161,
        timeout: float = 1.0,
        retries: int = 1,
    ) -> None:
        self.host = host
        self.community = community
        self.version = version
        self.port = port
        self.timeout = timeout
        self.retries = retries

    def get(self, oids: List[str]) -> Dict[str, object]:
        return snmp_get(
            self.host,
            oids,
            community=self.community,
            version=self.version,
            port=self.port,
            timeout=self.timeout,
            retries=self.retries,
        )

    def walk(self, base_oid: str, *, max_rows: int = 512) -> Dict[str, object]:
        return snmp_walk(
            self.host,
            base_oid,
            community=self.community,
            version=self.version,
            port=self.port,
            timeout=self.timeout,
            retries=self.retries,
            max_rows=max_rows,
        )
