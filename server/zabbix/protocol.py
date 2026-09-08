"""Протокол траппера Zabbix — чистые функции, ни одного сокета.

Формат снят с настоящего ``zabbix_sender`` 7.0.30 и зафиксирован тестами
(``tests/test_zabbix_wire.py`` гоняет официальный бинарник против нашего приёмника):

    ZBXD | flags(1 байт) | length(LE uint32) | reserved(LE uint32) | JSON-тело
    {"request":"sender data","data":[{"host":..,"key":..,"value":..}],"clock":..,"ns":..}

Ответ приходит в той же рамке; тело —
``{"response":"success","info":"processed: N; failed: M; total: T; seconds spent: X"}``.
Имён отвергнутых узлов Zabbix не сообщает — только числа (отсюда разбор в
``server/zabbix/export.py``).

Все значения на проводе — строки, даже числовые: тип элемента данных знает Zabbix,
а не отправитель.
"""

from __future__ import annotations

import json
import re
import struct
import zlib
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Optional, Sequence

HEADER = b"ZBXD"
HEADER_LEN = 13  # 4 (ZBXD) + 1 (flags) + 4 (length) + 4 (reserved)
FLAG_ZABBIX = 0x01
FLAG_COMPRESSED = 0x02

# Настоящий zabbix_sender шлёт ровно по 250 значений на соединение (проверено
# на живом бинарнике: 1200 значений -> 250,250,250,250,200). Держим тот же шаг:
# он заведомо приемлем для любого сервера Zabbix.
BATCH_SIZE = 250

# Потолок ответа. Он объявляется в заголовке ДО чтения тела, поэтому враждебный
# (или просто не тот) собеседник не может заставить нас выделить память.
MAX_RESPONSE_BYTES = 1024 * 1024

# Кап значения. Русская фраза причины ограничивается здесь, чтобы длинный текст
# вердикта не раздувал пакет; Zabbix и сам режет значения по 64 КБ.
MAX_VALUE_CHARS = 255

# Кап текста info из ответа. Он доезжает до снимка статуса, до неаутентифицированного
# /api/v1/zabbix/status и до HTML карточки — мегабайтная строка оттуда уже не уйдёт
# до перезапуска процесса.
MAX_INFO_CHARS = 512

# Кап имени узла: предел имени хоста в Zabbix. Имя приходит из hostname агента,
# а /ingest в поставке не аутентифицирован — строка недоверенная.
MAX_HOST_CHARS = 128

_INFO_RE = re.compile(
    r"processed:\s*(?P<processed>\d+)"
    r"(?:;\s*failed:\s*(?P<failed>\d+))?"
    r"(?:;\s*total:\s*(?P<total>\d+))?"
)


class ProtocolError(Exception):
    """Ответ не соответствует протоколу траппера (или это вообще не Zabbix)."""


@dataclass(frozen=True)
class Item:
    """Одно значение: узел, ключ элемента данных, значение строкой."""

    host: str
    key: str
    value: str


@dataclass(frozen=True)
class Response:
    ok: bool
    info: str
    processed: Optional[int] = None
    failed: Optional[int] = None
    total: Optional[int] = None


def fmt_value(value: Any) -> str:
    """Значение -> строка для провода.

    Целые остаются целыми (``62.0`` -> ``"62"``: Zabbix-элемент типа «Числовой
    (целое)» не примет ``62.0``), дробные печатаются без экспоненты, текст
    режется по ``MAX_VALUE_CHARS``.
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        rounded = round(value, 2)
        if rounded == int(rounded):
            return str(int(rounded))
        return f"{rounded:.2f}".rstrip("0").rstrip(".")
    text = str(value)
    return text[:MAX_VALUE_CHARS]


def chunks(items: Sequence[Item], size: int = BATCH_SIZE) -> Iterator[list]:
    """Разбить значения на пачки по ``size`` — по одному соединению на пачку."""
    step = max(1, size)
    for start in range(0, len(items), step):
        yield list(items[start : start + step])


def build_packet(items: Iterable[Item], *, clock: int, ns: int) -> Optional[bytes]:
    """Собрать ZBXD-пакет. ``None``, если отправлять нечего."""
    data = [{"host": i.host, "key": i.key, "value": i.value} for i in items]
    if not data:
        return None
    body = json.dumps(
        {"request": "sender data", "data": data, "clock": clock, "ns": ns},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return HEADER + bytes([FLAG_ZABBIX]) + struct.pack("<II", len(body), 0) + body


def check_declared_length(length: int) -> None:
    """Проверить объявленную в заголовке длину ДО чтения тела."""
    if length < 0 or length > MAX_RESPONSE_BYTES:
        raise ProtocolError(f"ответ Zabbix объявляет неправдоподобную длину: {length}")


def decode_header(raw: bytes) -> tuple:
    """(flags, length, reserved) из первых 13 байт ответа."""
    if len(raw) < HEADER_LEN or raw[:4] != HEADER:
        raise ProtocolError("ответ не похож на Zabbix: нет заголовка ZBXD")
    flags = raw[4]
    length, reserved = struct.unpack("<II", raw[5:HEADER_LEN])
    check_declared_length(length)
    return flags, length, reserved


def _decompress(body: bytes) -> bytes:
    """Распаковать тело с ЖЁСТКИМ потолком на распакованный размер.

    ``zlib.decompress`` без ``max_length`` — zip-бомба: ~1 МБ на проводе (ровно
    предел ``MAX_RESPONSE_BYTES``) разворачивается в 1 ГиБ в памяти. Собеседник
    траппера не аутентифицирован и не шифруется, поэтому такой ответ подделывает
    любой, кто стоит между SRP и Zabbix.
    """
    unpacker = zlib.decompressobj()
    try:
        out = unpacker.decompress(body, MAX_RESPONSE_BYTES)
    except zlib.error as exc:
        raise ProtocolError(f"не удалось распаковать ответ Zabbix: {exc}") from exc
    if unpacker.unconsumed_tail or not unpacker.eof:
        raise ProtocolError("ответ Zabbix распаковывается в неправдоподобный объём")
    return out


def parse_response(raw: bytes) -> Response:
    """Разобрать целиком прочитанный ответ (заголовок + тело)."""
    flags, length, _reserved = decode_header(raw)
    body = raw[HEADER_LEN : HEADER_LEN + length]
    if len(body) < length:
        raise ProtocolError("ответ Zabbix оборван: тело короче объявленной длины")
    if flags & FLAG_COMPRESSED:
        body = _decompress(body)
    return parse_body(body)


def _count(raw: Optional[str]) -> Optional[int]:
    """Число из строки info — только правдоподобной длины.

    ``int()`` на строке из 50 000 цифр бросает ValueError (Python 3.11+) или
    разбирает её квадратично (3.9). Ни то ни другое не должно случаться из-за
    ответа, пришедшего из сети.
    """
    if not raw or len(raw) > 12:
        return None
    return int(raw)


def parse_body(body: bytes) -> Response:
    """Разобрать уже распакованное тело ответа.

    Ловит ЛЮБОЕ исключение разбора: тело пришло из сети, и список конкретных
    типов тут всегда оказывается неполным (RecursionError на вложенном JSON,
    ValueError на гигантском числе — обе воспроизведены в ревью).
    """
    try:
        obj = json.loads(body.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 -- разбор недоверенного ответа
        raise ProtocolError(f"ответ Zabbix не разбирается как JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ProtocolError("ответ Zabbix не является объектом JSON")
    # Кап текста И фильтр символов: строка info уезжает в last_error, оттуда
    # в снимок статуса, в неаутентифицированный /api/v1/zabbix/status и в HTML
    # карточки. Без фильтра одиночный суррогат (U+D800) роняет обе поверхности
    # UnicodeEncodeError'ом до перезапуска процесса, а перевод строки рисует
    # в журнале поддельную строку в формате настоящей.
    info = "".join(c for c in str(obj.get("info") or "") if c.isprintable())[:MAX_INFO_CHARS]
    ok = obj.get("response") == "success"
    match = _INFO_RE.search(info)
    if not match:
        return Response(ok=ok, info=info)
    return Response(
        ok=ok,
        info=info,
        processed=_count(match.group("processed")),
        failed=_count(match.group("failed")),
        total=_count(match.group("total")),
    )
