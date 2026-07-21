"""P1 ingest guards: idempotency dedup and per-device rate limiting.

Both use process-lifetime in-memory state.

Idempotency: a client-generated UUID4 key is checked read-only (``has_seen``)
before an envelope is processed, and recorded (``mark_seen``) only after
ingest succeeds. A business-validation failure (422) therefore never burns the
key, so a client retry with corrected content is processed instead of being
silently dropped as a false duplicate (stoperrors P1-3 — marking on receipt
made every 422 a permanent data loss). Accepted race: two requests carrying
the same key can both be processed if the second arrives before the first
finishes — the window is the full ingest_envelope() duration (DB writes +
rescore), not instant: milliseconds under the shipped async-rescore config
(server/config.json sets async_rescore=true), up to the O(n^2) synchronous
trend pass (pipeline.py) if a deployment runs the code default
(async_rescore=False, server/config.py). Either way the result is a benign
duplicate row — the store is append-only and scoring reads latest-wins — still
strictly better than the prior guaranteed permanent loss on any 422 retry.
State does not survive server restarts (a retry after restart re-processes —
acceptable because the append-only store is idempotent within any given
second via the autoincrement PK).

Rate-limit: sliding-window counter per device_id. Protects against a single
device flooding the server and monopolising the synchronous rescore lock.
State resets on restart (agents reconnecting burst naturally; the restart
itself provides staggering).
"""

from __future__ import annotations

import threading
import time
from typing import Optional

_dedup_lock = threading.Lock()
_seen_keys: dict[str, float] = {}  # idempotency_key → monotonic time of first receipt
_DEDUP_TTL_SEC: float = 300.0  # 5 min — wider than any agent retry window

_rate_lock = threading.Lock()
_device_windows: dict[str, list[float]] = {}  # device_id → recent request monotonic times
_RATE_WINDOW_SEC: float = 60.0
# 30/min is generous (typical: 4 msg_types × 2/min = 8/min); busts on first-run
# multi-message flood without blocking legitimate reconnect bursts.
_RATE_MAX_PER_WINDOW: int = 30

# §6 «отказы буфера» серверной стороной: отклонённые конверты по причинам.
# In-process счётчики (один uvicorn-воркер); тесты сбрасывают через reset-хук.
REJECT_COUNTS: dict[str, int] = {
    "auth": 0,
    "rate_limit": 0,
    "duplicate": 0,
    "invalid": 0,
    "too_large": 0,
}


def count_reject(reason: str) -> None:
    REJECT_COUNTS[reason] = REJECT_COUNTS.get(reason, 0) + 1


def has_seen(key: Optional[str]) -> bool:
    """True → this key was already recorded as successfully processed (duplicate).

    Read-only (no mutation) — safe to call before an envelope is processed.
    None key (old agent without idempotency support) always returns False.
    """
    if not key:
        return False
    with _dedup_lock:
        return key in _seen_keys


def mark_seen(key: Optional[str]) -> None:
    """Record key as successfully processed.  No-op for a falsy key.

    Call ONLY after the envelope has been durably ingested — see module
    docstring (stoperrors P1-3) for why marking before processing is wrong.
    """
    if not key:
        return
    now = time.monotonic()
    with _dedup_lock:
        # Opportunistic trim so the dict doesn't grow unbounded.
        if len(_seen_keys) > 50_000:
            cutoff = now - _DEDUP_TTL_SEC
            stale = [k for k, t in _seen_keys.items() if t < cutoff]
            for k in stale:
                del _seen_keys[k]
        _seen_keys[key] = now


def check_rate_limit(device_id: str) -> bool:
    """True → within limit (process it).  False → exceeded (return 429)."""
    now = time.monotonic()
    cutoff = now - _RATE_WINDOW_SEC
    with _rate_lock:
        times = [t for t in _device_windows.get(device_id, []) if t >= cutoff]
        if len(times) >= _RATE_MAX_PER_WINDOW:
            _device_windows[device_id] = times
            return False
        times.append(now)
        _device_windows[device_id] = times
        return True


def reset_guards() -> None:
    """Reset all in-memory state.  Tests only — never call from production code."""
    with _dedup_lock:
        _seen_keys.clear()
    with _rate_lock:
        _device_windows.clear()
    for k in REJECT_COUNTS:
        REJECT_COUNTS[k] = 0
