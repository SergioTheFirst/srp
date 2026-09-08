"""Сценарии отказа отправки в Zabbix.

Главное требование задачи: недоступный Zabbix не влияет на работу SRP. Значит
в каждом сценарии проверяется одно и то же — функция ВЕРНУЛАСЬ, исключение
наружу не ушло, причина названа по-русски, и следующая отправка работает.
"""

from __future__ import annotations

import time

import pytest
from server.zabbix import protocol as p
from server.zabbix import sender as snd

from tests.zabbix_trapper_stub import TrapperStub

ITEMS = [p.Item(host="BUH-01", key="srp.state", value="h0")]

# Порт 9 (discard) на loopback: обычно мгновенный отказ соединения. На машинах
# с перехватывающим VPN (tun2socks/Outline) тот же адрес вместо отказа молчит до
# таймаута -- поэтому тесты проверяют НЕ конкретный код ошибки, а то, что отправка
# завершилась ошибкой за ограниченное время и без исключения.
DEAD_PORT = 9
_NETWORK_KINDS = (snd.KIND_REFUSED, snd.KIND_NETWORK, snd.KIND_TIMEOUT)


def test_nobody_listening_returns_an_error_not_an_exception():
    started = time.monotonic()
    result = snd.send(ITEMS, host="127.0.0.1", port=DEAD_PORT, timeout_sec=2.0)
    elapsed = time.monotonic() - started

    assert result.ok is False
    assert result.error_kind in _NETWORK_KINDS
    assert "127.0.0.1:9" in result.error
    assert elapsed < 10.0, f"отправка висела {elapsed:.1f} с"


def test_unresolvable_name_is_named_as_such():
    result = snd.send(ITEMS, host="zabbix.invalid-tld-for-tests", port=10051, timeout_sec=2.0)

    assert result.ok is False
    assert result.error_kind in (snd.KIND_DNS, snd.KIND_NETWORK)


def test_silent_receiver_gives_up_on_timeout_and_does_not_hang():
    with TrapperStub("slow", delay_sec=30.0) as stub:
        started = time.monotonic()
        result = snd.send(ITEMS, host="127.0.0.1", port=stub.port, timeout_sec=1.0)
        elapsed = time.monotonic() - started

    assert result.ok is False
    assert result.error_kind == snd.KIND_TIMEOUT
    assert elapsed < 10.0, f"отправка висела {elapsed:.1f} с вместо ~1 с"


def test_connection_closed_without_a_reply_is_reported():
    with TrapperStub("close") as stub:
        result = snd.send(ITEMS, host="127.0.0.1", port=stub.port, timeout_sec=2.0)

    assert result.ok is False
    assert result.error_kind == snd.KIND_CLOSED
    assert "оборвал" in result.error


def test_response_failed_is_an_error_with_the_zabbix_text():
    with TrapperStub("failed") as stub:
        result = snd.send(ITEMS, host="127.0.0.1", port=stub.port, timeout_sec=2.0)

    assert result.ok is False
    assert result.error_kind == snd.KIND_REJECTED
    assert "cannot process" in result.error


def test_something_else_listening_on_the_port_is_not_mistaken_for_zabbix():
    with TrapperStub("garbage") as stub:
        result = snd.send(ITEMS, host="127.0.0.1", port=stub.port, timeout_sec=2.0)

    assert result.ok is False
    assert result.error_kind == snd.KIND_PROTOCOL
    assert "не похож на Zabbix" in result.error


def test_absurd_declared_response_length_is_rejected_before_allocating():
    with TrapperStub("oversize") as stub:
        result = snd.send(ITEMS, host="127.0.0.1", port=stub.port, timeout_sec=2.0)

    assert result.ok is False
    assert result.error_kind == snd.KIND_PROTOCOL


def test_partial_rejection_is_not_an_error_but_is_counted():
    items = [
        p.Item(host="GOOD", key="srp.state", value="h0"),
        p.Item(host="BAD", key="srp.state", value="h0"),
        p.Item(host="BAD", key="srp.damage", value="1"),
    ]
    with TrapperStub("partial", reject=lambda i: i["host"] == "BAD") as stub:
        result = snd.send(items, host="127.0.0.1", port=stub.port, timeout_sec=2.0)

    assert result.ok is True  # пакет доставлен; отвергнутые значения — не отказ связи
    assert (result.processed, result.failed) == (1, 2)


def test_failure_of_the_first_batch_stops_the_rest():
    """Если Zabbix недоступен, остальные пачки упрутся в тот же таймаут."""
    items = [p.Item(host=f"h{i}", key="srp.state", value="h0") for i in range(600)]
    result = snd.send(items, host="127.0.0.1", port=DEAD_PORT, timeout_sec=1.0)

    assert result.ok is False
    assert result.total == 250, "после первого отказа остальные пачки слаться не должны"


def test_total_time_budget_stops_a_very_long_fan_out():
    items = [p.Item(host=f"h{i}", key="srp.state", value="h0") for i in range(1000)]
    with TrapperStub("slow", delay_sec=0.4) as stub:
        result = snd.send(
            items,
            host="127.0.0.1",
            port=stub.port,
            timeout_sec=5.0,
            max_total_sec=0.5,
        )

    assert result.ok is False
    assert result.error_kind == snd.KIND_BUDGET


def test_the_next_send_works_after_a_failure():
    failed = snd.send(ITEMS, host="127.0.0.1", port=DEAD_PORT, timeout_sec=1.0)
    assert failed.ok is False

    with TrapperStub("ok") as stub:
        result = snd.send(ITEMS, host="127.0.0.1", port=stub.port, timeout_sec=2.0)

    assert result.ok is True and result.processed == 1


@pytest.mark.parametrize("mode", ["ok", "failed", "close", "garbage", "oversize"])
def test_no_scenario_ever_raises(mode):
    with TrapperStub(mode) as stub:
        result = snd.send(ITEMS, host="127.0.0.1", port=stub.port, timeout_sec=2.0)

    assert isinstance(result, snd.SendResult)


# --------------------------------------------------------------------------- #
# Враждебный собеседник (находки security-ревью)
# --------------------------------------------------------------------------- #
def test_a_byte_at_a_time_receiver_cannot_hold_the_thread_forever():
    """settimeout ограничивает паузу МЕЖДУ байтами, а не суммарное чтение."""
    import socket
    import struct
    import threading

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    srv.settimeout(5.0)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def drip():
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        with conn:
            try:
                conn.recv(65536)
                body = b'{"response":"success","info":"processed: 1"}'
                head = b"ZBXD\x01" + struct.pack("<II", len(body), 0)
                for byte in head + body:
                    if stop.is_set():
                        return
                    conn.sendall(bytes([byte]))
                    time.sleep(0.3)
            except OSError:
                return

    thread = threading.Thread(target=drip, daemon=True)
    thread.start()
    try:
        started = time.monotonic()
        result = snd.send(ITEMS, host="127.0.0.1", port=port, timeout_sec=1.0)
        elapsed = time.monotonic() - started
    finally:
        stop.set()
        srv.close()

    assert result.ok is False
    assert elapsed < 5.0, f"чтение тянулось {elapsed:.1f} с при таймауте 1 с"


def test_a_hostile_reply_never_escapes_as_an_exception(monkeypatch):
    """Контракт «никогда не бросает» держится по конструкции, а не по везению."""

    def boom(*_a, **_kw):
        raise RecursionError("глубоко вложенный JSON")

    monkeypatch.setattr(snd, "_read_response", boom)

    with TrapperStub("ok") as stub:
        result = snd.send(ITEMS, host="127.0.0.1", port=stub.port, timeout_sec=2.0)

    assert result.ok is False
    assert result.error_kind == snd.KIND_PROTOCOL
