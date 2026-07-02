"""Liveness-пинг: минимальный конверт без PowerShell и без сбора данных.

Единственная задача — обновить devices.last_seen на сервере, чтобы «offline»
на дашборде был виден за минуты (2 пропущенных пинга), а не за полный
4-часовой телеметрийный цикл. Ноль строк в БД, ноль source_health, ноль
рескоринга — см. серверную ветку liveness в server/pipeline.py.
"""

from __future__ import annotations

from client.collectors.sources import CollectorResult


def collect_liveness() -> CollectorResult:
    # Непустой payload обязателен: transport.send пропускает конверт, у которого
    # falsy payload И falsy source_health.
    return CollectorResult({"alive": True}, {})
