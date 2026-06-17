# Network Printer Monitoring — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Собрать телеметрию с самих сетевых принтеров (счётчики, расходники, бумага, ошибки, инвентарь) и обнаруживать их в сети, чтобы честно ответить «какой принтер сколько бумаги тратит» по аппаратному счётчику.

**Architecture:** Чистый stdlib-движок опроса (SNMP v1/v2c на сокетах + IPP/HTTP через `urllib`) в пакете `server/printers/`, запуск на сервере; переносим в агент позже без переписывания. Серверный сбор пишет прямо в БД, минуя контракт агента. Телеметрия информационная (не в trust/scoring).

**Tech Stack:** Python 3.9 stdlib (`socket`, `struct`, `urllib`, `asyncio`, `concurrent.futures`), SQLite (`server/db.py`), FastAPI + Jinja2 (`server/api.py`, `server/web/`), Plotly (дашборд), pytest.

**Spec:** `docs/superpowers/specs/2026-06-17-printer-monitoring-design.md`

---

## Декомпозиция и порядок
7 этапов, каждый = ветка → TDD (RED→GREEN) → гейт зелёный (`ruff`/`mypy`/`bandit`/`pytest` cov≥80% + `python smoke.py`) → ревью сабагентом (`security-reviewer` Opus для SNMP/сети/SQL; `code-reviewer` Sonnet иначе) → `merge --no-ff`. Push только по команде. CHANGELOG-строка в том же коммите. CONTINUITY.md обновляется в конце этапа.

| Этап | Подсистема | Зависит от | Даёт рабочее ПО |
|---|---|---|---|
| 0 | База OID (data + research) | — | `oids.py` с проверенными OID |
| 1 | stdlib SNMP-движок | 0 | `snmp_get`/`snmp_walk` к реальному принтеру |
| 2 | Драйвер-реестр + Printer-MIB + классификатор | 1 | `PrinterReading` с одного принтера через CLI-проба |
| 3 | Тихое обнаружение | — (||1) | список принтеров из спулера+ARP+конфиг |
| 4 | Планировщик + таблицы БД | 2,3 | сервер сам периодически опрашивает и хранит |
| 5 | Вендор-драйверы + IPP/HTTP fallback | 2 | покрытие HP/Xerox/Kyocera/Canon/Brother/Lexmark/Ricoh/Epson |
| 6 | Дашборд `/printers` + сверка с `print_jobs` | 4 | инженер видит всё в UI |
| 7 | *(за стоп-гейтом, опц.)* активный скан | 1 | обнаружение без печати, с письменным разрешением |

Phase 0 и Phase 1 расписаны по шагам ниже. Для Phase 2–7 даны файлы, единицы, тесты и критерии готовности; пошаговый TDD с полным кодом дописывается в начале этапа (после того как появятся реальная база OID из Phase 0 и сигнатуры движка из Phase 1).

---

## Карта файлов (создаётся/меняется)

| Файл | Ответственность | Ограничение |
|---|---|---|
| `server/printers/__init__.py` | пакет | stdlib-only |
| `server/printers/ber.py` | BER/ASN.1 кодек (length/TLV/INTEGER/OCTET STRING/NULL/OID) | stdlib-only, <300 строк |
| `server/printers/snmp.py` | SNMP v1/v2c: build GET/GETNEXT/GETBULK, parse response, socket-транспорт, ретраи, кэш | stdlib-only |
| `server/printers/oids.py` | единая база OID (стандарт + vendor), без дублей | данные |
| `server/printers/drivers/__init__.py` | реестр драйверов + выбор по `sysObjectID` | stdlib-only |
| `server/printers/drivers/standard.py` | generic Printer-MIB / HOST-RESOURCES профиль | stdlib-only |
| `server/printers/drivers/<vendor>.py` | по файлу на вендора (этап 5) | stdlib-only |
| `server/printers/models.py` | неизменяемые dataclass `PrinterReading`, `Supply`, `Tray`, `PrinterError` | stdlib-only |
| `server/printers/classify.py` | «принтер ≠ ПК» | stdlib-only |
| `server/printers/ipp.py`, `http_probe.py` | fallback-источники (этап 5) | stdlib-only (`urllib`) |
| `server/printers/discovery.py` | слияние/дедуп источников | stdlib-only |
| `server/printers/collector.py` | оркестрация проба→`PrinterReading` (приоритет источников) | stdlib-only |
| `server/printers/scheduler.py` | lifespan-loop + ThreadPoolExecutor | сервер |
| `server/printers/config.py` | интервал, community, версия, список IP, флаг скана | сервер |
| `server/db.py` | +таблицы `printers`/`printer_readings`, миграции, store/get | параметризованный SQL |
| `shared/schema.py` | +additive-optional поле подсказок печати (этап 3) | без bump CONTRACT_VERSION |
| `client/collectors/print_jobs.py` или новый `printer_ports.py` | агент читает `Get-Printer`/`Get-PrinterPort` (этап 3) | WinPS 5.1, stdlib |
| `server/api.py`, `server/web/dashboard.py`, `server/web/templates/printers.html`, `base.html` | страница/эндпоинты (этап 6) | autoescape + `srpEsc` |
| `tests/test_printer_*.py` | тесты по подсистемам | cov≥80% |

---

## Phase 0 — Единая база OID

**Files:**
- Create: `server/printers/__init__.py`
- Create: `server/printers/oids.py`
- Create: `tests/test_printer_oids.py`

**Источники для извлечения (обязательное условие пользователя — изучить до кода движка):**
`alfonsrv/printer-monitoring` (карта vendor-семейств OID — основа абстракции драйверов), `ynlamy/check_snmp_printer` (стандартный Printer-MIB набор), `torgeirl/printer_tools` (парсинг расходников), `vvalchev/printer-scanner` (обнаружение). Делегировать извлечение сабагенту (Explore/general-purpose): «выписать все OID + как парсятся значения, вернуть таблицу `имя→OID→тип→источник→заметка по парсингу», БЕЗ дампа исходников».

- [ ] **Step 1: Написать падающий тест структуры базы OID**

```python
# tests/test_printer_oids.py
from server.printers import oids

def test_standard_oids_present_and_typed():
    # Базовый Printer-MIB / MIB-II / HOST-RESOURCES — обязательны.
    assert oids.STANDARD["sys_descr"] == "1.3.6.1.2.1.1.1.0"
    assert oids.STANDARD["sys_object_id"] == "1.3.6.1.2.1.1.2.0"
    assert oids.STANDARD["sys_uptime"] == "1.3.6.1.2.1.1.3.0"
    assert oids.STANDARD["sys_name"] == "1.3.6.1.2.1.1.5.0"
    assert oids.STANDARD["prt_serial"] == "1.3.6.1.2.1.43.5.1.1.17.1"
    assert oids.STANDARD["prt_marker_life_count"] == "1.3.6.1.2.1.43.10.2.1.4.1.1"
    # Табличные базы (без индекса) — для walk.
    assert oids.TABLES["supply_level"] == "1.3.6.1.2.1.43.11.1.1.9"
    assert oids.TABLES["supply_max"] == "1.3.6.1.2.1.43.11.1.1.8"
    assert oids.TABLES["supply_desc"] == "1.3.6.1.2.1.43.11.1.1.6"
    assert oids.TABLES["input_current"] == "1.3.6.1.2.1.43.8.2.1.10"
    assert oids.TABLES["hr_printer_status"] == "1.3.6.1.2.1.25.3.5.1.1"

def test_no_duplicate_oid_strings_within_a_group():
    for group in (oids.STANDARD, oids.TABLES):
        values = list(group.values())
        assert len(values) == len(set(values))

def test_vendor_enterprise_map_resolves_prefix():
    # sysObjectID -> вендор по enterprise-префиксу 1.3.6.1.4.1.<N>
    assert oids.vendor_for_sysobjectid("1.3.6.1.4.1.11.2.3.9.1") == "hp"
    assert oids.vendor_for_sysobjectid("1.3.6.1.4.1.1347.1") == "kyocera"
    assert oids.vendor_for_sysobjectid("1.3.6.1.4.1.99999") is None
```

- [ ] **Step 2: Запустить — упадёт** — `python -m pytest tests/test_printer_oids.py -q` → FAIL (нет модуля `oids`).

- [ ] **Step 3: Реализовать `oids.py`** — наполнить из RFC 3805/2790/1213 + извлечённого из репозиториев. Минимальный костяк:

```python
# server/printers/oids.py
"""Единая база OID принтеров. Стандарт (Printer-MIB RFC3805, HOST-RESOURCES RFC2790,
MIB-II RFC1213) + vendor-префиксы. Дубли убраны (см. tests/test_printer_oids.py)."""
from typing import Dict, Optional

STANDARD: Dict[str, str] = {
    "sys_descr": "1.3.6.1.2.1.1.1.0",
    "sys_object_id": "1.3.6.1.2.1.1.2.0",
    "sys_uptime": "1.3.6.1.2.1.1.3.0",
    "sys_name": "1.3.6.1.2.1.1.5.0",
    "prt_general_serial": "1.3.6.1.2.1.43.5.1.1.17.1",
    "prt_serial": "1.3.6.1.2.1.43.5.1.1.17.1",
    "prt_marker_life_count": "1.3.6.1.2.1.43.10.2.1.4.1.1",
}
TABLES: Dict[str, str] = {  # базы табличных OID (далее walk по индексам)
    "supply_level": "1.3.6.1.2.1.43.11.1.1.9",
    "supply_max": "1.3.6.1.2.1.43.11.1.1.8",
    "supply_desc": "1.3.6.1.2.1.43.11.1.1.6",
    "supply_type": "1.3.6.1.2.1.43.11.1.1.5",
    "input_current": "1.3.6.1.2.1.43.8.2.1.10",
    "input_max": "1.3.6.1.2.1.43.8.2.1.9",
    "input_media": "1.3.6.1.2.1.43.8.2.1.12",
    "hr_printer_status": "1.3.6.1.2.1.25.3.5.1.1",
    "hr_detected_error": "1.3.6.1.2.1.25.3.5.1.2",
    "prt_alert_desc": "1.3.6.1.2.1.43.18.1.1.8",
    "if_phys_address": "1.3.6.1.2.1.2.2.1.6",
}
_VENDOR_ENTERPRISE: Dict[str, str] = {
    "11": "hp", "253": "xerox", "1347": "kyocera", "1602": "canon",
    "367": "ricoh", "2435": "brother", "641": "lexmark", "1248": "epson",
}

def vendor_for_sysobjectid(sysobjectid: str) -> Optional[str]:
    prefix = "1.3.6.1.4.1."
    if not sysobjectid.startswith(prefix):
        return None
    enterprise = sysobjectid[len(prefix):].split(".", 1)[0]
    return _VENDOR_ENTERPRISE.get(enterprise)
```

- [ ] **Step 4: Запустить — пройдёт** — `python -m pytest tests/test_printer_oids.py -q` → PASS.
- [ ] **Step 5: Гейт + коммит** — `ruff`/`mypy server shared`/`bandit`/полный `pytest`; затем:

```bash
git checkout -b feat/printers-phase0-oids
git add server/printers/__init__.py server/printers/oids.py tests/test_printer_oids.py docs/superpowers/specs/2026-06-17-printer-monitoring-design.md docs/superpowers/plans/2026-06-17-printer-monitoring.md
git commit -m "feat(printers): seed unified printer OID database (phase 0)"
```

**Критерий готовности этапа:** база OID наполнена из 4 репозиториев + RFC, дубли отсутствуют (тест), vendor-резолвинг работает. CHANGELOG: строка в Added.

---

## Phase 1 — stdlib SNMP-движок (v1/v2c)

**Files:**
- Create: `server/printers/ber.py`, `server/printers/snmp.py`
- Test: `tests/test_printer_ber.py`, `tests/test_printer_snmp.py`

### Task 1.1 — BER length codec
- [ ] **Step 1: Падающий тест**

```python
# tests/test_printer_ber.py
from server.printers import ber

def test_length_short_form():
    assert ber.encode_length(5) == b"\x05"
    assert ber.decode_length(b"\x05rest", 0) == (5, 1)

def test_length_long_form():
    assert ber.encode_length(200) == b"\x81\xc8"
    assert ber.encode_length(300) == b"\x82\x01\x2c"
    assert ber.decode_length(b"\x82\x01\x2c", 0) == (300, 3)
```

- [ ] **Step 2: Запустить — FAIL** — `python -m pytest tests/test_printer_ber.py -q`.
- [ ] **Step 3: Реализовать**

```python
# server/printers/ber.py
"""Минимальный BER/ASN.1-кодек под SNMP v1/v2c (только нужные типы)."""
from typing import List, Tuple

def encode_length(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = []
    while n > 0:
        body.insert(0, n & 0xFF)
        n >>= 8
    return bytes([0x80 | len(body)]) + bytes(body)

def decode_length(data: bytes, pos: int) -> Tuple[int, int]:
    first = data[pos]
    if first < 0x80:
        return first, 1
    num = first & 0x7F
    value = int.from_bytes(data[pos + 1 : pos + 1 + num], "big")
    return value, 1 + num
```

- [ ] **Step 4: PASS** — `python -m pytest tests/test_printer_ber.py -q`.

### Task 1.2 — TLV + примитивы + OID
- [ ] **Step 1: Падающий тест**

```python
def test_encode_integer_and_octet_string():
    assert ber.encode_integer(0) == b"\x02\x01\x00"
    assert ber.encode_integer(127) == b"\x02\x01\x7f"
    assert ber.encode_octet_string(b"public") == b"\x04\x06public"
    assert ber.encode_null() == b"\x05\x00"

def test_encode_decode_oid_roundtrip():
    oid = "1.3.6.1.2.1.1.5.0"
    enc = ber.encode_oid(oid)
    assert enc[0] == 0x06
    assert ber.decode_oid(enc[2:]) == oid

def test_decode_tlv_returns_tag_value_next():
    tag, value, nxt = ber.decode_tlv(b"\x02\x01\x2a", 0)
    assert tag == 0x02 and value == b"\x2a" and nxt == 3
```

- [ ] **Step 2: FAIL.**
- [ ] **Step 3: Реализовать** (добавить в `ber.py`)

```python
def encode_tlv(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + encode_length(len(body)) + body

def encode_integer(value: int) -> bytes:
    if value == 0:
        return encode_tlv(0x02, b"\x00")
    body = []
    v = value
    while v not in (0, -1):
        body.insert(0, v & 0xFF)
        v >>= 8
    if value > 0 and body[0] & 0x80:
        body.insert(0, 0x00)
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
    tag = data[pos]
    length, lsize = decode_length(data, pos + 1)
    start = pos + 1 + lsize
    return tag, data[start : start + length], start + length

def decode_sequence(body: bytes) -> List[Tuple[int, bytes]]:
    items, pos = [], 0
    while pos < len(body):
        tag, value, pos = decode_tlv(body, pos)
        items.append((tag, value))
    return items
```

- [ ] **Step 4: PASS.**

### Task 1.3 — Сборка GET-запроса
- [ ] **Step 1: Падающий тест** (round-trip: собрали → разобрали обратно)

```python
# tests/test_printer_snmp.py
from server.printers import snmp, ber

def test_build_get_is_decodable():
    pkt = snmp.build_request(snmp.GET, ["1.3.6.1.2.1.1.5.0"], community="public",
                             version=1, request_id=42)
    tag, msg_body, _ = ber.decode_tlv(pkt, 0)
    assert tag == 0x30  # SEQUENCE
    items = ber.decode_sequence(msg_body)
    assert items[0] == (0x02, b"\x01")          # version v2c
    assert items[1] == (0x04, b"public")        # community
    assert items[2][0] == snmp.GET              # PDU тег 0xA0
```

- [ ] **Step 2: FAIL.**
- [ ] **Step 3: Реализовать** (начало `snmp.py`)

```python
# server/printers/snmp.py
"""SNMP v1/v2c поверх stdlib (socket+ber). Только чтение: GET/GETNEXT/GETBULK, никогда SET."""
import socket
from typing import Dict, List, Optional, Tuple
from server.printers import ber

GET = 0xA0
GETNEXT = 0xA1
GETRESPONSE = 0xA2
GETBULK = 0xA5
_NO_SUCH_OBJECT, _NO_SUCH_INSTANCE, _END_OF_MIB = 0x80, 0x81, 0x82

def build_request(pdu_type: int, oids: List[str], *, community: str, version: int,
                  request_id: int, non_repeaters: int = 0, max_repetitions: int = 0) -> bytes:
    varbinds = b"".join(
        ber.encode_tlv(0x30, ber.encode_oid(o) + ber.encode_null()) for o in oids
    )
    vb_seq = ber.encode_tlv(0x30, varbinds)
    if pdu_type == GETBULK:
        pdu_body = (ber.encode_integer(request_id) + ber.encode_integer(non_repeaters)
                    + ber.encode_integer(max_repetitions) + vb_seq)
    else:
        pdu_body = (ber.encode_integer(request_id) + ber.encode_integer(0)
                    + ber.encode_integer(0) + vb_seq)
    pdu = ber.encode_tlv(pdu_type, pdu_body)
    msg = ber.encode_integer(version) + ber.encode_octet_string(community.encode()) + pdu
    return ber.encode_tlv(0x30, msg)
```

- [ ] **Step 4: PASS.**

### Task 1.4 — Разбор ответа (типы + исключения v2c)
- [ ] **Step 1: Падающий тест**

```python
def test_parse_response_extracts_varbinds():
    # Собираем валидный GetResponse с одним OCTET STRING и одним Counter32.
    resp = snmp.build_request(snmp.GETRESPONSE, [], community="public",
                              version=1, request_id=7)  # каркас перезапишем варбайндами
    name = "1.3.6.1.2.1.1.5.0"
    vb1 = ber.encode_tlv(0x30, ber.encode_oid(name) + ber.encode_octet_string(b"PRN-1"))
    vb2 = ber.encode_tlv(0x30, ber.encode_oid("1.3.6.1.2.1.43.10.2.1.4.1.1")
                         + ber.encode_tlv(0x41, b"\x27\x10"))  # Counter32=10000
    pdu_body = (ber.encode_integer(7) + ber.encode_integer(0) + ber.encode_integer(0)
                + ber.encode_tlv(0x30, vb1 + vb2))
    pdu = ber.encode_tlv(snmp.GETRESPONSE, pdu_body)
    msg = ber.encode_tlv(0x30, ber.encode_integer(1)
                         + ber.encode_octet_string(b"public") + pdu)
    parsed = snmp.parse_response(msg)
    assert parsed[name] == "PRN-1"
    assert parsed["1.3.6.1.2.1.43.10.2.1.4.1.1"] == 10000
```

- [ ] **Step 2: FAIL.**
- [ ] **Step 3: Реализовать** (`parse_response` в `snmp.py`): разобрать внешний SEQUENCE → PDU → варбайнды; на каждый OID распаковать значение по тегу: `0x02` INTEGER → int (signed), `0x04` OCTET STRING → str (latin-1/utf-8 best-effort), `0x06` OID → str, `0x40` IpAddress → dotted, `0x41/0x42/0x43/0x46` Counter32/Gauge/TimeTicks/Counter64 → int (unsigned), `0x05` NULL/`0x80-0x82` исключения → `None` (UNKNOWN, не выдумываем). Вернуть `Dict[str, object]`.
- [ ] **Step 4: PASS.**

### Task 1.5 — Транспорт `snmp_get` (UDP, таймаут, ретраи)
- [ ] **Step 1: Падающий тест** через локальный UDP-сервер-заглушку (поток отвечает заранее собранным GetResponse на любой запрос); проверить, что `snmp_get` возвращает разобранный dict, и что при «нет ответа» в срок отдаёт `{}` (а не виснет).
- [ ] **Step 2: FAIL.**
- [ ] **Step 3: Реализовать**

```python
def snmp_get(host: str, oids: List[str], *, community: str = "public", version: int = 1,
             port: int = 161, timeout: float = 1.0, retries: int = 1,
             request_id: int = 1) -> Dict[str, object]:
    pkt = build_request(GET, oids, community=community, version=version, request_id=request_id)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        for _ in range(retries + 1):
            try:
                sock.sendto(pkt, (host, port))
                data, _addr = sock.recvfrom(65535)
                return parse_response(data)
            except socket.timeout:
                continue
        return {}
    finally:
        sock.close()
```

- [ ] **Step 4: PASS.**

### Task 1.6 — `snmp_walk` (GETNEXT/GETBULK) для табличных OID
- [ ] **Шаги TDD:** тест — walk по базе `supply_desc` возвращает значения по индексам, останавливается на выходе за префикс или `endOfMibView`; реализация — цикл GETNEXT (v1) / GETBULK (v2c, `max_repetitions`), пока возвращаемый OID начинается с базового префикса; защита от зацикливания (лимит итераций). Сигнатура: `snmp_walk(host, base_oid, *, community, version, ...) -> Dict[str, object]` (ключ = полный OID).
- [ ] **Гейт + коммит** всего Phase 1:

```bash
git checkout -b feat/printers-phase1-snmp
git add server/printers/ber.py server/printers/snmp.py tests/test_printer_ber.py tests/test_printer_snmp.py
git commit -m "feat(printers): pure-stdlib SNMP v1/v2c engine (BER codec, GET/WALK, timeouts/retries)"
```

**Критерий готовности:** `snmp_get`/`snmp_walk` читают реальный принтер в лаборатории; только чтение (нет SET); таймауты не вешают; cov≥80% по `ber.py`/`snmp.py`. Ревью — `security-reviewer` (Opus): сокеты/таймауты/недоверенный ввод парсера.

---

## Phase 2 — Драйвер-реестр + Printer-MIB профиль + классификатор

**Files:** Create `server/printers/models.py`, `server/printers/drivers/__init__.py`, `server/printers/drivers/standard.py`, `server/printers/classify.py`, `server/printers/collector.py`; Test `tests/test_printer_models.py`, `tests/test_printer_driver_standard.py`, `tests/test_printer_classify.py`.

**Единицы и тесты (пошаговый TDD дописать в начале этапа):**
- `models.py` — неизменяемые `@dataclass(frozen=True)`: `Supply(name,type,level,max,percent,unit)`, `Tray(name,media,level,max,status)`, `PrinterError(code,description)`, `PrinterReading(ip,hostname,mac,vendor,model,serial,firmware,uptime,status,total_pages,color_pages,mono_pages,duplex_pages,supplies,trays,errors,source_protocol)`. Тест: percent считается из level/max, отрицательные сентинелы Printer-MIB (`-2 unknown`, `-3 some-remaining`) → `None`.
- `drivers/standard.py` — функция `read(snmp_session) -> PrinterReading` по `oids.STANDARD`+`oids.TABLES` (walk расходников/лотков/ошибок, сборка из индексов). Тест: на зафиксированном наборе ответов SNMP (мок-сессия) собирается ожидаемый `PrinterReading`.
- `drivers/__init__.py` — реестр: `get_driver(sys_object_id) -> driver` через `oids.vendor_for_sysobjectid`, fallback `standard`. Тест: HP sysObjectID → hp-драйвер (в этапе 5; пока fallback к standard), мусор → standard.
- `classify.py` — `is_printer(probe_result) -> bool`: True только при ответе ветки `1.3.6.1.2.1.43` / `hrDeviceType`=printer / printer-enterprise sysObjectID / IPP-признак. Тест: ПК (нет Printer-MIB) → False; принтер → True; один открытый порт → False.
- `collector.py` — `probe(ip, cfg) -> Optional[PrinterReading]`: SNMP-проба → classify → выбрать драйвер → `read`; источник = `"snmp"`. (IPP/HTTP fallback подключается в этапе 5.)

**Критерий готовности:** CLI-проба одного IP даёт корректный `PrinterReading`; ПК не классифицируется как принтер. Ревью `code-reviewer` (Sonnet) + `security-reviewer` если трогаем парсинг недоверенного.

---

## Phase 3 — Тихое обнаружение (спулер + ARP + список)

**Files:** Create `server/printers/discovery.py`, `server/printers/config.py`; Modify `shared/schema.py` (additive поле), `client/collectors/print_jobs.py` или новый `client/collectors/printer_ports.py`, `server/pipeline.py` (принять подсказки), `server/db.py` (сохранить hint-IP); Test `tests/test_printer_discovery.py`, `tests/test_printer_ports_collector.py`, `tests/test_contract_printer_ports.py`.

**Единицы и тесты:**
- Агент `printer_ports`: PowerShell **WinPS 5.1** `Get-Printer`/`Get-PrinterPort` → список `{name, host/ip}` (только RFC1918 IP пропускаем). Тест: парсинг вывода (фикстуры), нелокальные/не-IP отсеиваются, языконезависимо.
- Контракт: additive-optional `printer_ports: Optional[List[PrinterPortHint]]` в `HistoricalPayload` (cap `max_length`, generous backstop), **без bump CONTRACT_VERSION**. Тест: старый агент (None) валиден; новый — валиден; кап режет.
- `discovery.py` — `merge(sources) -> List[PrinterCandidate]`: объединить hint-IP агентов + ARP-снимки (`server/analytics/netmap`/`db.get_network_snapshots`) + конфиг-список; дедуп по серийнику>MAC>IP. Тест: один принтер из трёх источников → один кандидат.
- `config.py` — `printer_poll_interval`, `snmp_community`, `snmp_version`, `printer_static_ips`, `printer_active_scan=False`.

**Критерий готовности:** сервер строит дедуплицированный список принтеров без сканирования. Ревью `security-reviewer` (Opus): контракт/ingest/PowerShell/приватность.

---

## Phase 4 — Планировщик + таблицы БД

**Files:** Create `server/printers/scheduler.py`; Modify `server/db.py` (+`printers`,`printer_readings`, миграции, `store_printer_reading`, `get_printers`, `get_printer`, `get_printer_series`, добавить таблицы в `_DEVICE_TABLES`-аналог если нужно), `server/main.py` (lifespan loop), `server/config.py`; Test `tests/test_printer_db.py`, `tests/test_printer_scheduler.py`.

**Единицы и тесты:**
- БД: `printers` (latest inventory, ключ = printer_id из идентичности), `printer_readings` (append-only: скаляры `total_pages/color/mono/duplex/status/received_at` + `detail` JSON-блоб расходников/лотков/ошибок, валидированный перед записью). Параметризованный SQL; миграции additive+backfill идемпотентны; retention-кап на принтер. Тест: store→get round-trip, миграция на старой БД no-op/идемпотентна, retention.
- `scheduler.py` — `run_poll_cycle(candidates, cfg)`: `ThreadPoolExecutor` опрашивает кандидатов (таймаут/ретрай на хост), результаты → `store_printer_reading`; недоступный → запись со `status=UNKNOWN`. Тест: цикл на моках collector (часть отвечает, часть таймаутит) пишет ожидаемое; отмена через `CancelledError` чистая.
- `main.py` lifespan: запустить poll-loop рядом с retention sweep, `contextlib.suppress(CancelledError)`, guard внутри (транзиентная ошибка БД не роняет старт — урок device-ghost-cleanup).

**Критерий готовности:** запущенный сервер сам периодически опрашивает принтеры и копит историю. Ревью `security-reviewer` (Opus): SQL/loop/ресурсы.

---

## Phase 5 — Вендор-драйверы + IPP/HTTP fallback

**Files:** Create `server/printers/drivers/{hp,xerox,kyocera,canon,brother,lexmark,ricoh,epson}.py`, `server/printers/ipp.py`, `server/printers/http_probe.py`; Modify `server/printers/collector.py` (цепочка fallback), `server/printers/oids.py` (vendor-OID); Test `tests/test_printer_driver_<vendor>.py`, `tests/test_printer_ipp.py`, `tests/test_printer_http.py`.

**Единицы и тесты:**
- По драйверу на вендора: vendor-OID для счётчиков/расходников там, где стандарт пуст; парсеры значений (Brother hex-строки; Canon/Kyocera таблицы счётчиков обходом). Тест на зафиксированных ответах вендора → ожидаемый `PrinterReading`.
- `ipp.py` — `urllib` POST `Get-Printer-Attributes` (IPP, бинарный кодек заголовка) → модель/состояние/счётчики где есть. Тест на фикстуре IPP-ответа.
- `http_probe.py` — последний fallback: вытащить модель/счётчики из веб-страницы статуса (вендор-специфичные парсеры, строго ограниченные). Тест на сохранённых HTML.
- `collector.probe` — приоритет Printer-MIB → vendor OID → IPP → HTTP; первый успешный материальный результат побеждает; источник проставляется.

**Критерий готовности:** целевые вендоры дают счётчики/расходники; неизвестный вендор — через generic. Ревью `security-reviewer` (Opus): `urllib`-таймауты, SSRF-защита (только RFC1918), недоверенный парсинг.

---

## Phase 6 — Дашборд `/printers` + сверка с печатью

**Files:** Create `server/web/templates/printers.html`; Modify `server/api.py` (`GET /api/v1/printers`, `/api/v1/printers/{id}`), `server/web/dashboard.py` (рендер `/printers`), `server/web/templates/base.html` (nav-ссылка), при необходимости `server/db.py` (сверочный запрос с `print_jobs`); Test `tests/test_printers_page.py`, `tests/test_printers_api.py`, `tests/test_printer_reconcile.py`.

**Единицы и тесты:**
- API отдаёт инвентарь+последнее чтение+историю (latest-by-id), числа — числами; недоступный → UNKNOWN.
- `printers.html` — SSR (autoescape) + JSON-остров + Plotly (как `/print`); карточки: полоски расходников, счётчики, ошибки/лотки, инвентарь; история счётчиков; **сверка**: аппаратный счётчик ↔ сумма `print_jobs` по принтеру. RU-текст; машинные значения латиницей.
- **XSS-пин:** враждебные `model`/`hostname`/`supply_desc` (`</script><script>`, `<img onerror>`) → через autoescape (SSR) и `srpEsc` (любой JS-приёмник, в т.ч. Plotly tick/hover). Тест обязателен (`[[dashboard-xss-srpesc]]`).
- Рендер `/printers` = 200; nav-ссылка есть.

**Критерий готовности:** инженер видит принтеры, расходники, ошибки, историю и «бумагу по принтерам». Ревью: `security-reviewer` (Opus, XSS/SQL) + `code-reviewer`. CHANGELOG: Added. После этого этапа модуль закрывает поставленную цель.

---

## Phase 7 — *(За стоп-гейтом, опционально)* Активный скан

> **НЕ начинать без письменного разрешения по безопасности** (правило проекта: активный скан «выглядит как атака для EDR»). Существует черновик `docs/superpowers/specs/2026-06-11-network-phase3-active-scan.md` — согласовать подход.

**Files:** Create `server/printers/scan.py`; Modify `server/printers/discovery.py` (подключить как источник за флагом), `server/printers/config.py`. Test `tests/test_printer_scan.py`.

**Единицы:** перебор подсети (ограниченный конфиг-диапазон), проба портов 161/9100/631 с жёсткими таймаутами и предельной параллельностью, SNMP-broadcast; результат → `classify` → кандидаты. Выключено по умолчанию (`printer_active_scan=False`); включается только явным флагом + зафиксированным согласием. Ревью `security-reviewer` (Opus) — обязательно.

---

## Сводный критерий «готово» (для каждого этапа)
`ruff` + `mypy [server+shared+client]` + `bandit` + `pytest` cov≥80% ALL GREEN · `python smoke.py` OK · CHANGELOG-строка в том же коммите · CONTINUITY.md обновлён · ревью сабагентом пройдено · `merge --no-ff` (push по команде).

## Self-review (план против спеки)
- Покрытие спеки: §5 компоненты → этапы 1–5; §6 обнаружение → этап 3 (+7 за гейтом); §7 драйверы/OID → этапы 0,2,5; §8 хранение → этап 4; §9 дашборд → этап 6; §10 производительность → этапы 1,4; §11 классификация → этап 2; §12 trust → этапы 4,6 (UNKNOWN); §13 безопасность → этапы 1,5,7 + ревью; §14 тесты → в каждом этапе. Пробелов нет.
- Плейсхолдеров нет в Phase 0/1 (полный код). Phase 2–7 — намеренно task-level, пошаговый код пишется just-in-time (зависит от вывода Phase 0/1); это декомпозиция, а не «TODO».
- Согласованность имён: `PrinterReading`, `snmp_get`/`snmp_walk`/`build_request`, `oids.STANDARD/TABLES/vendor_for_sysobjectid`, `is_printer`, `probe` — едины между этапами.
