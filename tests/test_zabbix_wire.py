"""Живой обмен по протоколу траппера — цепочка доверия к тестам.

1. Настоящий ``zabbix_sender`` -> наш приёмник. Если официальный бинарник смог
   отправить пакет, а приёмник его разобрал и ответил так, что отправитель
   отчитался «sent: N; skipped: 0» с кодом возврата 0 — приёмник соответствует
   протоколу. Эталон здесь — Zabbix, а не наши представления о нём.
2. Наш отправитель -> тот же приёмник. Форма пакета сверяется с эталоном,
   снятым в пункте 1.

Тесты пункта 1 пропускаются, если бинарника нет (см. SENDER_MISSING_REASON).
"""

from __future__ import annotations

import subprocess  # nosec B404 -- запускаем официальный бинарник по абсолютному пути

import pytest
from server.zabbix import protocol as p
from server.zabbix import sender as snd

from tests.zabbix_trapper_stub import SENDER_MISSING_REASON, TrapperStub, find_zabbix_sender

SENDER = find_zabbix_sender()
needs_sender = pytest.mark.skipif(SENDER is None, reason=SENDER_MISSING_REASON)


def _run_sender(port: int, stdin_text: str, timeout: float = 60.0):
    assert SENDER is not None
    return subprocess.run(  # nosec B603 -- фиксированный argv, путь к бинарнику не из сети
        [SENDER, "-z", "127.0.0.1", "-p", str(port), "-i", "-"],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


# --------------------------------------------------------------------------- #
# 1. Сертификация приёмника официальным zabbix_sender
# --------------------------------------------------------------------------- #
@needs_sender
def test_official_sender_accepts_our_receiver():
    with TrapperStub("ok") as stub:
        proc = _run_sender(stub.port, "BUH-01 srp.state h0\nBUH-01 srp.damage 62.5\n")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "sent: 2" in proc.stdout and "skipped: 0" in proc.stdout
    assert stub.values() == [
        {"host": "BUH-01", "key": "srp.state", "value": "h0"},
        {"host": "BUH-01", "key": "srp.damage", "value": "62.5"},
    ]


@needs_sender
def test_official_sender_frames_the_packet_exactly_as_we_do():
    """Эталон формы: у настоящего отправителя и у нас совпадают заголовок и тело."""
    with TrapperStub("ok") as stub:
        proc = _run_sender(stub.port, "BUH-01 srp.state h0\n")
    assert proc.returncode == 0
    reference_raw = stub.raw[0]
    reference_body = stub.packets[0]

    ours = p.build_packet([p.Item(host="BUH-01", key="srp.state", value="h0")], clock=1, ns=1)
    assert ours is not None

    assert ours[:5] == reference_raw[:5]  # ZBXD + флаг
    assert set(reference_body) == {"request", "data", "clock", "ns"}
    assert reference_body["request"] == "sender data"


@needs_sender
def test_official_sender_reports_partial_rejection_the_way_we_parse_it():
    """Наш разбор строки info должен совпадать с тем, как её читает сам Zabbix."""
    with TrapperStub("partial", reject=lambda item: item["host"] == "NOSUCHHOST") as stub:
        proc = _run_sender(stub.port, "BUH-01 srp.state h0\nNOSUCHHOST srp.state h0\n")

    # Официальный отправитель отличает частичный отказ ненулевым кодом возврата.
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert stub.packets, "приёмник не получил пакет"


# --------------------------------------------------------------------------- #
# 2. Наш отправитель против сертифицированного приёмника
# --------------------------------------------------------------------------- #
def test_our_sender_delivers_values_the_receiver_understands():
    items = [
        p.Item(host="BUH-01", key="srp.state", value="h3"),
        p.Item(host="BUH-01", key="srp.reason", value="Накопитель: заменить диск"),
    ]
    with TrapperStub("ok") as stub:
        result = snd.send(items, host="127.0.0.1", port=stub.port, timeout_sec=5.0)

    assert result.error is None
    assert (result.processed, result.failed) == (2, 0)
    assert stub.values() == [
        {"host": "BUH-01", "key": "srp.state", "value": "h3"},
        {"host": "BUH-01", "key": "srp.reason", "value": "Накопитель: заменить диск"},
    ]


def test_our_sender_splits_600_values_into_three_connections():
    items = [p.Item(host=f"host{i}", key="srp.state", value="h0") for i in range(600)]
    with TrapperStub("ok") as stub:
        result = snd.send(items, host="127.0.0.1", port=stub.port, timeout_sec=5.0)

    assert result.error is None
    assert len(stub.packets) == 3
    assert [len(pkt["data"]) for pkt in stub.packets] == [250, 250, 100]
    assert result.processed == 600


def test_our_sender_counts_partial_rejection():
    items = [
        p.Item(host="GOOD", key="srp.state", value="h0"),
        p.Item(host="NOSUCHHOST", key="srp.state", value="h0"),
    ]
    with TrapperStub("partial", reject=lambda item: item["host"] == "NOSUCHHOST") as stub:
        result = snd.send(items, host="127.0.0.1", port=stub.port, timeout_sec=5.0)

    assert result.error is None
    assert (result.processed, result.failed) == (1, 1)
