"""TCP-клиент траппера Zabbix. Никогда не бросает наружу.

Контракт: ``send()`` возвращает ``SendResult`` всегда — с ``error=None`` при успехе
и с человекочитаемой русской причиной в ``error`` при любом отказе. Недоступный,
молчащий, оборвавший соединение или подменённый Zabbix не должен влиять на работу
SRP, поэтому здесь нет ни одного пути, ведущего к исключению у вызывающего.

Все операции с сокетом ограничены таймаутом (и соединение, и отправка, и чтение),
а общая длительность отправки — ``max_total_sec``: без этого «Zabbix не влияет
на SRP» было бы неправдой, потому что поток из пула ``asyncio.to_thread`` завис бы
навсегда.
"""

from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass
from typing import Optional, Sequence

from server.zabbix import protocol

log = logging.getLogger("srp.zabbix")

# Общий потолок одной отправки парка. Пачек может быть много (250 значений в
# каждой); без этого потолка недоступный Zabbix растянул бы цикл на минуты.
DEFAULT_MAX_TOTAL_SEC = 60.0

# Машинные виды отказа: по ним подавляется повтор одинаковых строк в журнале.
KIND_DNS = "dns"
KIND_REFUSED = "refused"
KIND_TIMEOUT = "timeout"
KIND_CLOSED = "closed"
KIND_PROTOCOL = "protocol"
KIND_NETWORK = "network"
KIND_REJECTED = "rejected"
KIND_BUDGET = "budget"


@dataclass(frozen=True)
class SendResult:
    processed: int = 0
    failed: int = 0
    total: int = 0
    error: Optional[str] = None
    error_kind: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _recv_exactly(sock: socket.socket, n: int, deadline: float) -> bytes:
    """Прочитать ровно n байт, но не дольше общего дедлайна.

    ``settimeout`` ограничивает паузу МЕЖДУ байтами, а не суммарное чтение:
    собеседник, отдающий по байту раз в 4.9 с при таймауте 5 с, растягивал бы
    чтение мегабайтного ответа на месяцы и держал бы поток из пула
    ``asyncio.to_thread`` занятым навсегда (воспроизведено в ревью).
    """
    buf = b""
    while len(buf) < n:
        left = deadline - time.monotonic()
        if left <= 0:
            raise socket.timeout("исчерпан общий бюджет чтения ответа")
        sock.settimeout(left)
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionResetError("соединение закрыто до конца ответа")
        buf += chunk
    return buf


def _read_response(sock: socket.socket, deadline: float) -> protocol.Response:
    header = _recv_exactly(sock, protocol.HEADER_LEN, deadline)
    _flags, length, _reserved = protocol.decode_header(header)
    body = _recv_exactly(sock, length, deadline) if length else b""
    return protocol.parse_response(header + body)


def send_batch(
    items: Sequence[protocol.Item],
    *,
    host: str,
    port: int,
    timeout_sec: float,
    clock: Optional[int] = None,
) -> SendResult:
    """Отправить одну пачку значений одним соединением. Не бросает."""
    now = time.time()
    packet = protocol.build_packet(
        items, clock=int(clock if clock is not None else now), ns=int((now % 1) * 1e9)
    )
    if packet is None:
        return SendResult()
    target = f"{host}:{port}"
    deadline = time.monotonic() + timeout_sec
    try:
        with socket.create_connection((host, port), timeout=timeout_sec) as sock:
            sock.settimeout(timeout_sec)
            sock.sendall(packet)
            response = _read_response(sock, deadline)
    except socket.gaierror as exc:
        return SendResult(
            total=len(items),
            error=f"имя {host} не разрешается: {exc}",
            error_kind=KIND_DNS,
        )
    except socket.timeout:
        return SendResult(
            total=len(items),
            error=f"Zabbix {target} не ответил за {timeout_sec:g} с",
            error_kind=KIND_TIMEOUT,
        )
    except ConnectionRefusedError:
        return SendResult(
            total=len(items),
            error=f"Zabbix {target} недоступен: соединение отклонено",
            error_kind=KIND_REFUSED,
        )
    except ConnectionResetError:
        return SendResult(
            total=len(items),
            error=f"Zabbix {target} оборвал соединение",
            error_kind=KIND_CLOSED,
        )
    except protocol.ProtocolError as exc:
        return SendResult(
            total=len(items),
            error=f"ответ не похож на Zabbix ({target}): {exc}",
            error_kind=KIND_PROTOCOL,
        )
    except OSError as exc:
        return SendResult(
            total=len(items),
            error=f"Zabbix {target} недоступен: {exc}",
            error_kind=KIND_NETWORK,
        )
    except Exception as exc:  # noqa: BLE001 -- контракт «никогда не бросает наружу»
        # Список конкретных типов на разборе недоверенного ответа всегда неполон
        # (RecursionError на вложенном JSON, ValueError на гигантском числе).
        # Здесь этот контракт держится по конструкции, а не по везению.
        log.exception("неожиданная ошибка при отправке в Zabbix %s", target)
        return SendResult(
            total=len(items),
            error=f"неожиданная ошибка при отправке в Zabbix {target}: {exc!s:.200}",
            error_kind=KIND_PROTOCOL,
        )
    if not response.ok:
        return SendResult(
            total=len(items),
            error=f"Zabbix отверг пакет: {response.info or 'без пояснения'}",
            error_kind=KIND_REJECTED,
        )
    processed = response.processed if response.processed is not None else len(items)
    failed = response.failed if response.failed is not None else 0
    total = response.total if response.total is not None else len(items)
    return SendResult(processed=processed, failed=failed, total=total)


def send(
    items: Sequence[protocol.Item],
    *,
    host: str,
    port: int,
    timeout_sec: float,
    batch_size: int = protocol.BATCH_SIZE,
    max_total_sec: float = DEFAULT_MAX_TOTAL_SEC,
) -> SendResult:
    """Отправить значения пачками. Первый же отказ прекращает отправку.

    Прекращаем на первой ошибке намеренно: если Zabbix недоступен, остальные
    пачки упрутся в тот же таймаут и растянут цикл — а результат уже известен.
    """
    processed = failed = total = 0
    deadline = time.monotonic() + max_total_sec
    for batch in protocol.chunks(items, batch_size):
        if time.monotonic() > deadline:
            return SendResult(
                processed=processed,
                failed=failed,
                total=total,
                error=f"отправка прервана: не уложились в {max_total_sec:g} с",
                error_kind=KIND_BUDGET,
            )
        result = send_batch(batch, host=host, port=port, timeout_sec=timeout_sec)
        processed += result.processed
        failed += result.failed
        total += result.total
        if result.error:
            return SendResult(
                processed=processed,
                failed=failed,
                total=total,
                error=result.error,
                error_kind=result.error_kind,
            )
    return SendResult(processed=processed, failed=failed, total=total)
