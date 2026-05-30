"""SQLite storage for SRP (MVP).

One file DB, zero-config. Latest-wins for slow-changing data (inventory,
historical, scores); append+cap for time series (heartbeats, events).

All queries are parameterized. Table names in prune helpers are module
constants, never user input.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

_db_path: Optional[Path] = None
_retain_hb = 500
_retain_ev = 1000
_lock = threading.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
  device_id     TEXT PRIMARY KEY,
  hostname      TEXT,
  manufacturer  TEXT,
  model         TEXT,
  chassis       TEXT,
  agent_version TEXT,
  first_seen    TEXT,
  last_seen     TEXT
);
CREATE TABLE IF NOT EXISTS inventory (
  device_id TEXT PRIMARY KEY,
  ts        TEXT,
  payload   TEXT
);
CREATE TABLE IF NOT EXISTS historical (
  device_id TEXT PRIMARY KEY,
  ts        TEXT,
  payload   TEXT
);
CREATE TABLE IF NOT EXISTS heartbeats (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT,
  ts        TEXT,
  payload   TEXT
);
CREATE INDEX IF NOT EXISTS idx_hb_device ON heartbeats(device_id, id);
CREATE TABLE IF NOT EXISTS events (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT,
  ts        TEXT,
  log       TEXT,
  source    TEXT,
  event_id  INTEGER,
  level     TEXT,
  message   TEXT
);
CREATE INDEX IF NOT EXISTS idx_ev_device ON events(device_id, id);
CREATE TABLE IF NOT EXISTS scores (
  device_id     TEXT PRIMARY KEY,
  ts            TEXT,
  performance   REAL,
  reliability   REAL,
  wear          REAL,
  risk_exposure REAL,
  risk          TEXT
);
CREATE TABLE IF NOT EXISTS source_last_good (
  device_id TEXT,
  source    TEXT,
  reading   TEXT,
  ts        TEXT,
  PRIMARY KEY (device_id, source)
);
CREATE TABLE IF NOT EXISTS trust (
  device_id TEXT PRIMARY KEY,
  ts        TEXT,
  result    TEXT
);
"""


def init_db(db_path: Path, retain_heartbeats: int = 500, retain_events: int = 1000) -> None:
    global _db_path, _retain_hb, _retain_ev
    _db_path = Path(db_path)
    _retain_hb = retain_heartbeats
    _retain_ev = retain_events
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(_SCHEMA)


def _connect() -> sqlite3.Connection:
    if _db_path is None:
        raise RuntimeError("db not initialized; call init_db() first")
    conn = sqlite3.connect(str(_db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# --------------------------------------------------------------------------- #
# Writes
# --------------------------------------------------------------------------- #
def upsert_device(
    device_id: str,
    ts: str,
    agent_version: str,
    hostname: Optional[str] = None,
    manufacturer: Optional[str] = None,
    model: Optional[str] = None,
    chassis: Optional[str] = None,
) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO devices
              (device_id, hostname, manufacturer, model, chassis,
               agent_version, first_seen, last_seen)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(device_id) DO UPDATE SET
              hostname     = COALESCE(excluded.hostname, devices.hostname),
              manufacturer = COALESCE(excluded.manufacturer, devices.manufacturer),
              model        = COALESCE(excluded.model, devices.model),
              chassis      = COALESCE(excluded.chassis, devices.chassis),
              agent_version= excluded.agent_version,
              last_seen    = excluded.last_seen
            """,
            (device_id, hostname, manufacturer, model, chassis, agent_version, ts, ts),
        )


def touch_device(device_id: str, ts: str, agent_version: str) -> None:
    """Ensure a device row exists and bump last_seen (for heartbeat/events)."""
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO devices (device_id, agent_version, first_seen, last_seen)
            VALUES (?,?,?,?)
            ON CONFLICT(device_id) DO UPDATE SET last_seen = excluded.last_seen
            """,
            (device_id, agent_version, ts, ts),
        )


def store_inventory(device_id: str, ts: str, payload: dict[str, Any]) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO inventory (device_id, ts, payload) VALUES (?,?,?)
            ON CONFLICT(device_id) DO UPDATE SET ts=excluded.ts, payload=excluded.payload
            """,
            (device_id, ts, json.dumps(payload)),
        )


def store_historical(device_id: str, ts: str, payload: dict[str, Any]) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO historical (device_id, ts, payload) VALUES (?,?,?)
            ON CONFLICT(device_id) DO UPDATE SET ts=excluded.ts, payload=excluded.payload
            """,
            (device_id, ts, json.dumps(payload)),
        )


def store_heartbeat(device_id: str, ts: str, payload: dict[str, Any]) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO heartbeats (device_id, ts, payload) VALUES (?,?,?)",
            (device_id, ts, json.dumps(payload)),
        )
        conn.execute(
            """DELETE FROM heartbeats WHERE device_id=? AND id NOT IN (
                 SELECT id FROM heartbeats WHERE device_id=? ORDER BY id DESC LIMIT ?)""",
            (device_id, device_id, _retain_hb),
        )


def store_events(device_id: str, events: list[dict[str, Any]]) -> None:
    if not events:
        return
    with _lock, _connect() as conn:
        conn.executemany(
            """INSERT INTO events (device_id, ts, log, source, event_id, level, message)
               VALUES (?,?,?,?,?,?,?)""",
            [
                (
                    device_id,
                    e.get("ts"),
                    e.get("log"),
                    e.get("source"),
                    e.get("event_id"),
                    e.get("level"),
                    (e.get("message") or "")[:500],
                )
                for e in events
            ],
        )
        conn.execute(
            """DELETE FROM events WHERE device_id=? AND id NOT IN (
                 SELECT id FROM events WHERE device_id=? ORDER BY id DESC LIMIT ?)""",
            (device_id, device_id, _retain_ev),
        )


def store_scores(device_id: str, ts: str, scores: dict[str, Any]) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO scores
              (device_id, ts, performance, reliability, wear, risk_exposure, risk)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(device_id) DO UPDATE SET
              ts=excluded.ts, performance=excluded.performance,
              reliability=excluded.reliability, wear=excluded.wear,
              risk_exposure=excluded.risk_exposure, risk=excluded.risk
            """,
            (
                device_id,
                ts,
                scores.get("performance"),
                scores.get("reliability"),
                scores.get("wear"),
                scores.get("risk_exposure"),
                json.dumps(scores.get("risk", {})),
            ),
        )


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #
def _load(conn: sqlite3.Connection, table: str, device_id: str) -> Optional[dict]:
    row = conn.execute(
        # B608: {table} is a fixed module literal, never user input.
        f"SELECT ts, payload FROM {table} WHERE device_id=?",  # nosec B608
        (device_id,),
    ).fetchone()
    if row is None:
        return None
    return {"ts": row["ts"], **json.loads(row["payload"])}


def get_devices() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT d.device_id, d.hostname, d.model, d.chassis, d.last_seen,
                   s.performance, s.reliability, s.wear, s.risk_exposure, s.risk
            FROM devices d LEFT JOIN scores s ON s.device_id = d.device_id
            ORDER BY COALESCE(s.risk_exposure, 0) DESC, d.last_seen DESC
            """
        ).fetchall()
    out = []
    for r in rows:
        risk = json.loads(r["risk"]) if r["risk"] else {}
        out.append(
            {
                "device_id": r["device_id"],
                "hostname": r["hostname"],
                "model": r["model"],
                "chassis": r["chassis"],
                "last_seen": r["last_seen"],
                "performance": r["performance"],
                "reliability": r["reliability"],
                "wear": r["wear"],
                "risk_exposure": r["risk_exposure"],
                "top_risk": _top_risk(risk),
            }
        )
    return out


def _top_risk(risk: dict[str, Any]) -> Optional[dict[str, Any]]:
    classes = risk.get("classes") if isinstance(risk, dict) else None
    if not classes:
        return None
    top = max(classes, key=lambda c: c.get("probability", 0))
    return {"name": top.get("name"), "probability": top.get("probability")}


def get_device(device_id: str) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        d = conn.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
        if d is None:
            return None
        inventory = _load(conn, "inventory", device_id)
        historical = _load(conn, "historical", device_id)
        hb_row = conn.execute(
            "SELECT ts, payload FROM heartbeats WHERE device_id=? ORDER BY id DESC LIMIT 1",
            (device_id,),
        ).fetchone()
        latest_hb = {"ts": hb_row["ts"], **json.loads(hb_row["payload"])} if hb_row else None
        ev_rows = conn.execute(
            """SELECT ts, log, source, event_id, level, message
               FROM events WHERE device_id=? ORDER BY id DESC LIMIT 50""",
            (device_id,),
        ).fetchall()
        s = conn.execute("SELECT * FROM scores WHERE device_id=?", (device_id,)).fetchone()

    scores = None
    if s is not None:
        scores = {
            "ts": s["ts"],
            "performance": s["performance"],
            "reliability": s["reliability"],
            "wear": s["wear"],
            "risk_exposure": s["risk_exposure"],
            "risk": json.loads(s["risk"]) if s["risk"] else {},
        }
    return {
        "device_id": d["device_id"],
        "hostname": d["hostname"],
        "manufacturer": d["manufacturer"],
        "model": d["model"],
        "chassis": d["chassis"],
        "agent_version": d["agent_version"],
        "first_seen": d["first_seen"],
        "last_seen": d["last_seen"],
        "inventory": inventory,
        "historical": historical,
        "latest_heartbeat": latest_hb,
        "events": [dict(r) for r in ev_rows],
        "scores": scores,
    }


def get_inventory(device_id: str) -> Optional[dict]:
    with _connect() as conn:
        return _load(conn, "inventory", device_id)


def get_historical(device_id: str) -> Optional[dict]:
    with _connect() as conn:
        return _load(conn, "historical", device_id)


def get_recent_heartbeats(device_id: str, limit: int = 20) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ts, payload FROM heartbeats WHERE device_id=? ORDER BY id DESC LIMIT ?",
            (device_id, limit),
        ).fetchall()
    return [{"ts": r["ts"], **json.loads(r["payload"])} for r in rows]


def count_recent_events(device_id: str, event_ids: list[int]) -> int:
    if not event_ids:
        return 0
    placeholders = ",".join("?" for _ in event_ids)
    with _connect() as conn:
        row = conn.execute(
            # B608: placeholders are only "?" marks; all values are parameterized.
            f"SELECT COUNT(*) AS n FROM events WHERE device_id=? AND event_id IN ({placeholders})",  # nosec B608
            (device_id, *event_ids),
        ).fetchone()
    return int(row["n"])


# --------------------------------------------------------------------------- #
# Telemetry-trust helpers (Plan 3)
# --------------------------------------------------------------------------- #
def set_last_good(device_id: str, source: str, reading: dict[str, Any], ts: str) -> None:
    """Upsert the latest known-good reading for a (device, source) pair.

    Future semantic validators (frozen-value, impossible-delta) read this to
    compare incoming telemetry against the previous accepted sample.
    """
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO source_last_good (device_id, source, reading, ts)
            VALUES (?,?,?,?)
            ON CONFLICT(device_id, source) DO UPDATE SET
              reading = excluded.reading,
              ts      = excluded.ts
            """,
            (device_id, source, json.dumps(reading), ts),
        )


def get_last_good(device_id: str, source: str) -> Optional[dict]:
    """Return the last good reading for a (device, source) pair, or None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT reading FROM source_last_good WHERE device_id=? AND source=?",
            (device_id, source),
        ).fetchone()
    if row is None:
        return None
    return json.loads(row["reading"])


def store_trust(device_id: str, ts: str, result: dict[str, Any]) -> None:
    """Upsert the latest trust result (per-domain states + lineage) for a device."""
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO trust (device_id, ts, result) VALUES (?,?,?)
            ON CONFLICT(device_id) DO UPDATE SET
              ts     = excluded.ts,
              result = excluded.result
            """,
            (device_id, ts, json.dumps(result)),
        )


def get_trust(device_id: str) -> Optional[dict]:
    """Return the latest trust result for a device, or None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT result FROM trust WHERE device_id=?",
            (device_id,),
        ).fetchone()
    if row is None:
        return None
    return json.loads(row["result"])
