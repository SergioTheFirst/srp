"""Telemetry collectors: each builds one typed payload from local Windows state."""

from client.collectors.events import collect_events
from client.collectors.heartbeat import collect_heartbeat
from client.collectors.historical import collect_historical
from client.collectors.inventory import collect_inventory
from client.collectors.liveness import collect_liveness

__all__ = [
    "collect_inventory",
    "collect_historical",
    "collect_heartbeat",
    "collect_events",
    "collect_liveness",
]
