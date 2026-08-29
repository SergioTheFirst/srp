"""Shared message contract between SRP client agent and server.

Versioned feature contract per Part 3 (C3.4): the agent and server agree on a
stable set of message shapes. Payload models use ``extra="allow"`` so a newer
agent can add fields without breaking an older server (forward-compatible).
"""

from shared.schema import (
    CONTRACT_VERSION,
    Envelope,
    EventBatchPayload,
    HeartbeatPayload,
    HistoricalPayload,
    InventoryPayload,
)

__all__ = [
    "Envelope",
    "InventoryPayload",
    "HistoricalPayload",
    "HeartbeatPayload",
    "EventBatchPayload",
    "CONTRACT_VERSION",
]
