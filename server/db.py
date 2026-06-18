"""SQLite storage for SRP (MVP).

One file DB, zero-config. Latest-wins for slow-changing identity (inventory,
devices); append+cap longitudinal history for everything time-varying
(heartbeats, events, historical, scores). History is the P0 foundation for
trend detection ("is it getting worse?") and future label loops -- overwriting
latest-wins would erase the very signal early-warning depends on (W0.1).

All queries are parameterized. Table names in schema/prune/migration helpers are
module constants, never user input.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_db_path: Optional[Path] = None
_retain_hb = 500
_retain_ev = 1000
_retain_hist = 2000  # historical readings kept per device (W0.1; downsample TBD)
_retain_scores = 5000  # computed-score rows kept per device (W0.1; downsample TBD)
_CLOCK_DRIFT_FLAG_SEC = 300  # |received_at - ts| above this (s) flags clock drift (W0.2)
_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
  device_id       TEXT PRIMARY KEY,
  hostname        TEXT,
  manufacturer    TEXT,
  model           TEXT,
  chassis         TEXT,
  agent_version   TEXT,
  first_seen      TEXT,
  last_seen       TEXT,
  site_code       TEXT,
  site_name       TEXT,
  org_code        TEXT,
  dept_code       TEXT,
  comment         TEXT,
  last_reported_ts TEXT,
  clock_drift_sec REAL
);
CREATE TABLE IF NOT EXISTS inventory (
  device_id TEXT PRIMARY KEY,
  ts        TEXT,
  payload   TEXT
);
CREATE TABLE IF NOT EXISTS historical (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id    TEXT,
  ts           TEXT,
  payload      TEXT,
  received_at  TEXT,
  clock_drift_sec REAL
);
CREATE INDEX IF NOT EXISTS idx_hist_device ON historical(device_id, id);
CREATE TABLE IF NOT EXISTS heartbeats (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id    TEXT,
  ts           TEXT,
  payload      TEXT,
  received_at  TEXT,
  clock_drift_sec REAL
);
CREATE INDEX IF NOT EXISTS idx_hb_device ON heartbeats(device_id, id);
CREATE TABLE IF NOT EXISTS events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id    TEXT,
  ts           TEXT,
  log          TEXT,
  source       TEXT,
  event_id     INTEGER,
  level        TEXT,
  message      TEXT,
  received_at  TEXT,
  clock_drift_sec REAL
);
CREATE INDEX IF NOT EXISTS idx_ev_device ON events(device_id, id);
CREATE TABLE IF NOT EXISTS scores (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id     TEXT,
  ts            TEXT,
  performance   REAL,
  reliability   REAL,
  wear          REAL,
  risk_exposure REAL,
  risk          TEXT
);
CREATE INDEX IF NOT EXISTS idx_scores_device ON scores(device_id, id);
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
CREATE TABLE IF NOT EXISTS acknowledgements (
  device_id TEXT PRIMARY KEY,
  note      TEXT,
  acked_at  TEXT
);
CREATE TABLE IF NOT EXISTS device_source_trust (
  device_id         TEXT,
  source            TEXT,
  state             TEXT,
  weight            REAL,
  collector_status  TEXT,
  semantic_status   TEXT,
  reason            TEXT,
  ts                TEXT,
  PRIMARY KEY (device_id, source)
);
CREATE TABLE IF NOT EXISTS print_jobs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id   TEXT NOT NULL,
  job_id      INTEGER,
  ts          TEXT NOT NULL,
  received_at TEXT,
  printer     TEXT,
  user_name   TEXT,
  pages       INTEGER,
  size_bytes  INTEGER,
  source      TEXT
);
CREATE INDEX IF NOT EXISTS idx_print_device_ts ON print_jobs(device_id, ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_print_dedup
  ON print_jobs(device_id, job_id) WHERE job_id IS NOT NULL;
"""


def init_db(
    db_path: Path,
    retain_heartbeats: int = 500,
    retain_events: int = 1000,
    retain_historical: int = 2000,
    retain_scores: int = 5000,
) -> None:
    global _db_path, _retain_hb, _retain_ev, _retain_hist, _retain_scores
    _db_path = Path(db_path)
    _retain_hb = retain_heartbeats
    _retain_ev = retain_events
    _retain_hist = retain_historical
    _retain_scores = retain_scores
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        _migrate_legacy_latest_wins(conn)
        conn.executescript(_SCHEMA)
        _migrate_add_columns(conn)


def _connect() -> sqlite3.Connection:
    if _db_path is None:
        raise RuntimeError("db not initialized; call init_db() first")
    conn = sqlite3.connect(str(_db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# --------------------------------------------------------------------------- #
# Schema migration (pre-W0.1 latest-wins -> append-only)
# --------------------------------------------------------------------------- #
_APPEND_ONLY_TABLES = ("historical", "scores")

# Legacy historical/scores used PRIMARY KEY(device_id) (<=1 row per device), so
# copying every row into the new id-keyed shape is lossless.
_REBUILD: dict[str, str] = {
    "historical": """
        DROP TABLE IF EXISTS historical__new;
        BEGIN;
        CREATE TABLE historical__new (
          id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT, ts TEXT, payload TEXT);
        INSERT INTO historical__new (device_id, ts, payload)
          SELECT device_id, ts, payload FROM historical;
        DROP TABLE historical;
        ALTER TABLE historical__new RENAME TO historical;
        COMMIT;
    """,
    "scores": """
        DROP TABLE IF EXISTS scores__new;
        BEGIN;
        CREATE TABLE scores__new (
          id INTEGER PRIMARY KEY AUTOINCREMENT, device_id TEXT, ts TEXT,
          performance REAL, reliability REAL, wear REAL, risk_exposure REAL, risk TEXT);
        INSERT INTO scores__new
          (device_id, ts, performance, reliability, wear, risk_exposure, risk)
          SELECT device_id, ts, performance, reliability, wear, risk_exposure, risk FROM scores;
        DROP TABLE scores;
        ALTER TABLE scores__new RENAME TO scores;
        COMMIT;
    """,
}


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


def _has_id_column(conn: sqlite3.Connection, table: str) -> bool:
    # PRAGMA cannot be parameterized; enforce the constant-table invariant so the
    # f-string can never interpolate caller-controlled input.
    if table not in _APPEND_ONLY_TABLES:
        raise ValueError(f"unknown table for migration check: {table!r}")
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == "id" for r in rows)


def _migrate_legacy_latest_wins(conn: sqlite3.Connection) -> None:
    """Rebuild pre-W0.1 historical/scores (PRIMARY KEY device_id, no `id`) into
    the append-only id-keyed shape, preserving rows. No-op on fresh DBs (tables
    absent -> created by the schema) and on already-migrated DBs."""
    for table in _APPEND_ONLY_TABLES:
        if not _table_exists(conn, table):
            continue
        if _has_id_column(conn, table):
            continue
        conn.executescript(_REBUILD[table])


# Additive W0.2 columns. CREATE TABLE IF NOT EXISTS will not add columns to an
# existing table, so pre-W0.2 DBs need an explicit ALTER. Table + column names
# below are fixed module literals, never user input.
_ADD_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "historical": (("received_at", "TEXT"), ("clock_drift_sec", "REAL")),
    "heartbeats": (("received_at", "TEXT"), ("clock_drift_sec", "REAL")),
    "events": (("received_at", "TEXT"), ("clock_drift_sec", "REAL")),
    "devices": (
        ("last_reported_ts", "TEXT"),
        ("clock_drift_sec", "REAL"),
        ("org_code", "TEXT"),
        ("dept_code", "TEXT"),
        ("comment", "TEXT"),
        ("department", "TEXT"),
    ),
    "print_jobs": (("source", "TEXT"),),
}
_BACKFILL: dict[str, str] = {
    # Pre-W0.2 rows carry no server stamp; best-effort backfill from the client ts
    # (devices: from last_seen) so staleness/windows keep a usable value.
    "historical": "UPDATE historical SET received_at = ts WHERE received_at IS NULL",
    "heartbeats": "UPDATE heartbeats SET received_at = ts WHERE received_at IS NULL",
    "events": "UPDATE events SET received_at = ts WHERE received_at IS NULL",
    "devices": "UPDATE devices SET last_reported_ts = last_seen WHERE last_reported_ts IS NULL",
    # Pre-fallback rows could only have come from the Event 307 collector.
    "print_jobs": "UPDATE print_jobs SET source = 'events' WHERE source IS NULL",
}


def _migrate_add_columns(conn: sqlite3.Connection) -> None:
    """Add W0.2 received_at / clock-drift columns to pre-W0.2 tables, then backfill.

    No-op on fresh DBs (the schema already creates the columns) and idempotent on
    already-migrated DBs (present columns are skipped; backfill is WHERE ... IS NULL).
    """
    for table, cols in _ADD_COLUMNS.items():
        if not _table_exists(conn, table):
            continue
        # PRAGMA/ALTER cannot be parameterized; *table*/*col* are fixed literals above.
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}  # nosec B608
        added = False
        for col, col_type in cols:
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_type}")  # nosec B608
                added = True
        if added:
            conn.execute(_BACKFILL[table])


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
    site_code: Optional[str] = None,
    site_name: Optional[str] = None,
    org_code: Optional[str] = None,
    dept_code: Optional[str] = None,
    comment: Optional[str] = None,
    received_at: Optional[str] = None,
    last_reported_ts: Optional[str] = None,
    clock_drift_sec: Optional[float] = None,
) -> None:
    recv = received_at or _now_iso()  # server receipt = staleness anchor (W0.2)
    reported = last_reported_ts or ts  # client self-reported time (compat)
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO devices
              (device_id, hostname, manufacturer, model, chassis,
               agent_version, first_seen, last_seen,
               site_code, site_name, org_code, dept_code, comment,
               last_reported_ts, clock_drift_sec)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(device_id) DO UPDATE SET
              hostname     = COALESCE(excluded.hostname, devices.hostname),
              manufacturer = COALESCE(excluded.manufacturer, devices.manufacturer),
              model        = COALESCE(excluded.model, devices.model),
              chassis      = COALESCE(excluded.chassis, devices.chassis),
              agent_version= excluded.agent_version,
              last_seen    = excluded.last_seen,
              site_code    = COALESCE(excluded.site_code, devices.site_code),
              site_name    = COALESCE(excluded.site_name, devices.site_name),
              org_code     = COALESCE(excluded.org_code, devices.org_code),
              dept_code    = COALESCE(excluded.dept_code, devices.dept_code),
              comment      = COALESCE(excluded.comment, devices.comment),
              last_reported_ts = excluded.last_reported_ts,
              clock_drift_sec  = excluded.clock_drift_sec
            """,
            (
                device_id,
                hostname,
                manufacturer,
                model,
                chassis,
                agent_version,
                recv,
                recv,
                site_code,
                site_name,
                org_code,
                dept_code,
                comment,
                reported,
                clock_drift_sec,
            ),
        )


def touch_device(
    device_id: str,
    ts: str,
    agent_version: str,
    site_code: Optional[str] = None,
    site_name: Optional[str] = None,
    org_code: Optional[str] = None,
    dept_code: Optional[str] = None,
    comment: Optional[str] = None,
    received_at: Optional[str] = None,
    last_reported_ts: Optional[str] = None,
    clock_drift_sec: Optional[float] = None,
) -> None:
    """Ensure a device row exists and bump last_seen (for heartbeat/events).

    last_seen is the server receipt time (W0.2): staleness must not depend on the
    client clock. last_reported_ts retains the client's self-reported time.
    """
    recv = received_at or _now_iso()
    reported = last_reported_ts or ts
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO devices
              (device_id, agent_version, first_seen, last_seen,
               site_code, site_name, org_code, dept_code, comment,
               last_reported_ts, clock_drift_sec)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(device_id) DO UPDATE SET
              last_seen = excluded.last_seen,
              site_code  = COALESCE(excluded.site_code, devices.site_code),
              site_name  = COALESCE(excluded.site_name, devices.site_name),
              org_code   = COALESCE(excluded.org_code, devices.org_code),
              dept_code  = COALESCE(excluded.dept_code, devices.dept_code),
              comment    = COALESCE(excluded.comment, devices.comment),
              last_reported_ts = excluded.last_reported_ts,
              clock_drift_sec  = excluded.clock_drift_sec
            """,
            (
                device_id,
                agent_version,
                recv,
                recv,
                site_code,
                site_name,
                org_code,
                dept_code,
                comment,
                reported,
                clock_drift_sec,
            ),
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


def store_historical(
    device_id: str,
    ts: str,
    payload: dict[str, Any],
    received_at: Optional[str] = None,
    clock_drift_sec: Optional[float] = None,
) -> None:
    recv = received_at or _now_iso()
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO historical (device_id, ts, payload, received_at, clock_drift_sec) "
            "VALUES (?,?,?,?,?)",
            (device_id, ts, json.dumps(payload), recv, clock_drift_sec),
        )
        conn.execute(
            """DELETE FROM historical WHERE device_id=? AND id NOT IN (
                 SELECT id FROM historical WHERE device_id=? ORDER BY id DESC LIMIT ?)""",
            (device_id, device_id, _retain_hist),
        )


def store_heartbeat(
    device_id: str,
    ts: str,
    payload: dict[str, Any],
    received_at: Optional[str] = None,
    clock_drift_sec: Optional[float] = None,
) -> None:
    recv = received_at or _now_iso()
    with _lock, _connect() as conn:
        conn.execute(
            "INSERT INTO heartbeats (device_id, ts, payload, received_at, clock_drift_sec) "
            "VALUES (?,?,?,?,?)",
            (device_id, ts, json.dumps(payload), recv, clock_drift_sec),
        )
        conn.execute(
            """DELETE FROM heartbeats WHERE device_id=? AND id NOT IN (
                 SELECT id FROM heartbeats WHERE device_id=? ORDER BY id DESC LIMIT ?)""",
            (device_id, device_id, _retain_hb),
        )


def store_events(
    device_id: str,
    events: list[dict[str, Any]],
    received_at: Optional[str] = None,
    clock_drift_sec: Optional[float] = None,
) -> None:
    if not events:
        return
    recv = received_at or _now_iso()  # batch receipt = window anchor (W0.2)
    with _lock, _connect() as conn:
        conn.executemany(
            """INSERT INTO events
                 (device_id, ts, log, source, event_id, level, message,
                  received_at, clock_drift_sec)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            [
                (
                    device_id,
                    e.get("ts"),
                    e.get("log"),
                    e.get("source"),
                    e.get("event_id"),
                    e.get("level"),
                    (e.get("message") or "")[:500],
                    recv,
                    clock_drift_sec,
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
        conn.execute(
            """DELETE FROM scores WHERE device_id=? AND id NOT IN (
                 SELECT id FROM scores WHERE device_id=? ORDER BY id DESC LIMIT ?)""",
            (device_id, device_id, _retain_scores),
        )


# --------------------------------------------------------------------------- #
# Deletes / cleanup (device-ghost hygiene, 2026-06-16)
# --------------------------------------------------------------------------- #
# Every table that carries a device_id. delete_device MUST clear all of them, or
# an orphan shard is left behind = a new kind of garbage. The introspection test
# in tests/test_device_cleanup.py fails if a future per-device table is added but
# not registered here.
_DEVICE_TABLES: tuple[str, ...] = (
    "inventory",
    "historical",
    "heartbeats",
    "events",
    "scores",
    "source_last_good",
    "trust",
    "acknowledgements",
    "device_source_trust",
    "print_jobs",
    "devices",
)

_SECONDS_PER_DAY = 86_400


def _delete_device_rows(conn: sqlite3.Connection, device_id: str) -> None:
    """Delete every row for *device_id* across all per-device tables."""
    for table in _DEVICE_TABLES:
        # B608: table names are fixed module literals, never user input.
        conn.execute(f"DELETE FROM {table} WHERE device_id=?", (device_id,))  # nosec B608


def delete_device(device_id: str) -> bool:
    """Remove a device and ALL its data in one transaction.

    Returns True if the device existed (so a route can answer 404 otherwise).
    """
    with _lock, _connect() as conn:
        existed = (
            conn.execute("SELECT 1 FROM devices WHERE device_id=?", (device_id,)).fetchone()
            is not None
        )
        _delete_device_rows(conn, device_id)
    return existed


def purge_devices_silent_for(days: int, *, dry_run: bool = False) -> dict[str, Any]:
    """Delete devices whose server-stamped ``last_seen`` is older than *days*.

    ``last_seen`` is the server receipt time (W0.2), so silence is judged on the
    server clock, never the client's. ``dry_run=True`` returns the candidate ids
    without deleting (preview for the dashboard / for logging). A device whose
    ``last_seen`` is absent or unparseable is left untouched -- we never delete
    what we cannot age-judge.
    """
    if days < 0:
        raise ValueError("days must be >= 0")
    cutoff_sec = days * _SECONDS_PER_DAY
    with _lock, _connect() as conn:
        rows = conn.execute("SELECT device_id, last_seen FROM devices").fetchall()
        ids = [
            r["device_id"]
            for r in rows
            if (age := _age_seconds(r["last_seen"])) is not None and age >= cutoff_sec
        ]
        if not dry_run:
            for device_id in ids:
                _delete_device_rows(conn, device_id)
    return {"count": len(ids), "device_ids": ids, "deleted": not dry_run}


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


def _latest_historical(conn: sqlite3.Connection, device_id: str) -> Optional[dict]:
    """Newest historical reading for a device (append-only -> order by id desc)."""
    row = conn.execute(
        "SELECT ts, payload FROM historical WHERE device_id=? ORDER BY id DESC LIMIT 1",
        (device_id,),
    ).fetchone()
    if row is None:
        return None
    return {"ts": row["ts"], **json.loads(row["payload"])}


_STALE_AFTER_SEC = 900  # no contact for >15 min -> "stale" (agent silent / box off)
STALE_AFTER_SEC = _STALE_AFTER_SEC  # public alias for dashboard
_CERT_SOON_DAYS = 30  # certificate expiring within 30 days is flagged


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _age_seconds(iso: Optional[str]) -> Optional[int]:
    dt = _parse_iso(iso)
    return None if dt is None else int((datetime.now(timezone.utc) - dt).total_seconds())


def age_seconds(iso: Optional[str]) -> Optional[int]:
    """Public wrapper for dashboard / routes."""
    return _age_seconds(iso)


def _days_until(iso: Optional[str]) -> Optional[int]:
    dt = _parse_iso(iso)
    return None if dt is None else (dt - datetime.now(timezone.utc)).days


def _risk_alerts(risk: dict[str, Any]) -> tuple[Optional[str], int, int]:
    """(device_trust, count of UNKNOWN domains, count of regressed sources)."""
    domains = risk.get("domains") or {}
    unknown = sum(1 for d in domains.values() if d.get("state") == "unknown")
    regressed = len(risk.get("regressed_sources") or [])
    return risk.get("device_trust"), unknown, regressed


def _cert_summary(hist_payload: Optional[str]) -> tuple[Optional[int], bool]:
    """(min days-to-expiry across active machine + personal certs, any expiring < 30d).

    Folds both machine certs (``certificates``, seen by the SYSTEM agent) and the
    tray-spooled personal certs (``user_certificates``) so the fleet column matches
    the per-cert blocks on the device card; the soonest expiry wins.
    """
    if not hist_payload:
        return None, False
    try:
        payload = json.loads(hist_payload)
        certs = (payload.get("certificates") or []) + (payload.get("user_certificates") or [])
    except (ValueError, AttributeError):
        return None, False
    # Only active (not-yet-expired) certs; expired ones are excluded from the fleet column.
    days = [d for d in (_days_until(c.get("not_after")) for c in certs) if d is not None and d >= 0]
    if not days:
        return None, False
    lo = min(days)
    return lo, lo < _CERT_SOON_DAYS


def set_ack(device_id: str, note: str, ts: str) -> None:
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO acknowledgements (device_id, note, acked_at) VALUES (?,?,?)
            ON CONFLICT(device_id) DO UPDATE SET note=excluded.note, acked_at=excluded.acked_at
            """,
            (device_id, note, ts),
        )


def get_ack(device_id: str) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT note, acked_at FROM acknowledgements WHERE device_id=?", (device_id,)
        ).fetchone()
    return {"note": row["note"], "acked_at": row["acked_at"]} if row else None


def get_devices() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT d.device_id, d.hostname, d.model, d.chassis, d.last_seen,
                   d.site_code, d.site_name, d.org_code, d.dept_code, d.comment,
                   d.department, d.last_reported_ts, d.clock_drift_sec,
                   s.performance, s.reliability, s.wear, s.risk_exposure, s.risk,
                   h.payload AS hist_payload,
                   a.note AS ack_note, a.acked_at AS ack_at
            FROM devices d
            LEFT JOIN scores s ON s.device_id = d.device_id
              AND s.id = (SELECT MAX(id) FROM scores WHERE device_id = d.device_id)
            LEFT JOIN historical h ON h.device_id = d.device_id
              AND h.id = (SELECT MAX(id) FROM historical WHERE device_id = d.device_id)
            LEFT JOIN acknowledgements a ON a.device_id = d.device_id
            ORDER BY COALESCE(s.risk_exposure, 0) DESC, d.last_seen DESC
            """
        ).fetchall()
    out = []
    for r in rows:
        risk = json.loads(r["risk"]) if r["risk"] else {}
        device_trust, unknown_domains, regressed_count = _risk_alerts(risk)
        cert_min_days, cert_expiring = _cert_summary(r["hist_payload"])
        age = _age_seconds(r["last_seen"])
        worsening_count, trajectory_risk = _trajectory_summary(risk)
        out.append(
            {
                "device_id": r["device_id"],
                "hostname": r["hostname"],
                "model": r["model"],
                "chassis": r["chassis"],
                "last_seen": r["last_seen"],
                "last_seen_age_sec": age,
                "stale": age is not None and age > _STALE_AFTER_SEC,
                "last_reported_ts": r["last_reported_ts"],
                "clock_drift_sec": r["clock_drift_sec"],
                "clock_drift": r["clock_drift_sec"] is not None
                and abs(r["clock_drift_sec"]) > _CLOCK_DRIFT_FLAG_SEC,
                "site_code": r["site_code"],
                "site_name": r["site_name"],
                "org_code": r["org_code"],
                "dept_code": r["dept_code"],
                "comment": r["comment"],
                "department": r["department"],
                "performance": r["performance"],
                "reliability": r["reliability"],
                "wear": r["wear"],
                "risk_exposure": r["risk_exposure"],
                "top_risk": _top_risk(risk),
                "device_trust": device_trust,
                "unknown_domains": unknown_domains,
                "regressed_count": regressed_count,
                "cert_min_days": cert_min_days,
                "cert_expiring": cert_expiring,
                "worsening_count": worsening_count,
                "trajectory_risk": trajectory_risk,
                "ack": {"note": r["ack_note"], "acked_at": r["ack_at"]} if r["ack_at"] else None,
            }
        )
    return out


def _top_risk(risk: dict[str, Any]) -> Optional[dict[str, Any]]:
    classes = risk.get("classes") if isinstance(risk, dict) else None
    if not classes:
        return None
    top = max(classes, key=lambda c: c.get("probability", 0))
    return {"name": top.get("name"), "probability": top.get("probability")}


def _trajectory_summary(risk: dict[str, Any]) -> tuple[int, Optional[float]]:
    """(count of worsening trajectory axes, trajectory_risk score 0-100 | None)."""
    trajectory = risk.get("trajectory") or {}
    worsening = sum(
        1 for v in trajectory.values() if isinstance(v, dict) and v.get("direction") == "worsening"
    )
    score100 = risk.get("score100") if isinstance(risk, dict) else None
    traj = (score100 or {}).get("trajectory_risk") if isinstance(score100, dict) else None
    traj_risk: Optional[float] = traj.get("value") if isinstance(traj, dict) else None
    return worsening, traj_risk


def get_device(device_id: str) -> Optional[dict[str, Any]]:
    with _connect() as conn:
        d = conn.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
        if d is None:
            return None
        inventory = _load(conn, "inventory", device_id)
        historical = _latest_historical(conn, device_id)
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
        s = conn.execute(
            "SELECT * FROM scores WHERE device_id=? ORDER BY id DESC LIMIT 1", (device_id,)
        ).fetchone()

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
        "site_code": d["site_code"],
        "site_name": d["site_name"],
        "org_code": d["org_code"],
        "dept_code": d["dept_code"],
        "comment": d["comment"],
        "agent_version": d["agent_version"],
        "first_seen": d["first_seen"],
        "last_seen": d["last_seen"],
        "last_reported_ts": d["last_reported_ts"],
        "clock_drift_sec": d["clock_drift_sec"],
        "clock_drift": d["clock_drift_sec"] is not None
        and abs(d["clock_drift_sec"]) > _CLOCK_DRIFT_FLAG_SEC,
        "inventory": inventory,
        "historical": historical,
        "latest_heartbeat": latest_hb,
        "events": [dict(r) for r in ev_rows],
        "scores": scores,
        "department": d["department"],
        "ack": get_ack(device_id),
    }


def get_inventory(device_id: str) -> Optional[dict]:
    with _connect() as conn:
        return _load(conn, "inventory", device_id)


def get_historical(device_id: str) -> Optional[dict]:
    with _connect() as conn:
        return _latest_historical(conn, device_id)


def get_historical_series(device_id: str, limit: int = 100) -> list[dict]:
    """Historical readings for a device, newest-first (append-only time series)."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT ts, received_at, clock_drift_sec, payload
               FROM historical WHERE device_id=? ORDER BY id DESC LIMIT ?""",
            (device_id, limit),
        ).fetchall()
    return [
        {
            "ts": r["ts"],
            "received_at": r["received_at"],
            "clock_drift_sec": r["clock_drift_sec"],
            **json.loads(r["payload"]),
        }
        for r in rows
    ]


def get_score_series(device_id: str, limit: int = 100) -> list[dict]:
    """Computed scores for a device, newest-first (append-only time series)."""
    with _connect() as conn:
        rows = conn.execute(
            """SELECT ts, performance, reliability, wear, risk_exposure, risk
               FROM scores WHERE device_id=? ORDER BY id DESC LIMIT ?""",
            (device_id, limit),
        ).fetchall()
    return [
        {
            "ts": r["ts"],
            "performance": r["performance"],
            "reliability": r["reliability"],
            "wear": r["wear"],
            "risk_exposure": r["risk_exposure"],
            "risk": json.loads(r["risk"]) if r["risk"] else {},
        }
        for r in rows
    ]


def get_recent_heartbeats(device_id: str, limit: int = 20) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT ts, received_at, clock_drift_sec, payload
               FROM heartbeats WHERE device_id=? ORDER BY id DESC LIMIT ?""",
            (device_id, limit),
        ).fetchall()
    return [
        {
            "ts": r["ts"],
            "received_at": r["received_at"],
            "clock_drift_sec": r["clock_drift_sec"],
            **json.loads(r["payload"]),
        }
        for r in rows
    ]


def get_network_snapshots() -> list[dict[str, Any]]:
    """Latest network snapshot per device (map + subnet-anomaly read side, D7).

    One fleet query (latest-by-id, same pattern as get_devices); devices whose
    latest historical carries no network fields are skipped.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT d.device_id, d.hostname, d.site_code, d.site_name, d.last_seen,
                   h.payload AS hist_payload
            FROM devices d
            JOIN historical h ON h.device_id = d.device_id
              AND h.id = (SELECT MAX(id) FROM historical WHERE device_id = d.device_id)
            """
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        payload = json.loads(r["hist_payload"]) if r["hist_payload"] else {}
        # Defensive read-side caps (security review): the contract lists carry no
        # max_length yet, and build_netmap work scales with fleet x list sizes --
        # one bloated payload must not slow every page view. Proper boundary fix
        # (schema max_length) is a future contract PR.
        adapters = (payload.get("network_adapters") or [])[:64]
        neighbors = (payload.get("network_neighbors") or [])[:512]
        quality = (payload.get("network_quality") or [])[:16]
        if not (adapters or neighbors or quality):
            continue
        out.append(
            {
                "device_id": r["device_id"],
                "hostname": r["hostname"],
                "site_code": r["site_code"],
                "site_name": r["site_name"],
                "last_seen": r["last_seen"],
                "adapters": adapters,
                "neighbors": neighbors,
                "quality": quality,
            }
        )
    return out


def get_printer_port_hints() -> list[dict[str, Any]]:
    """Latest spooler printer-port hints across the fleet (read side for discovery).

    Mirrors get_network_snapshots: one fleet query (latest-by-id), extract the
    additive ``printer_ports`` list from each device's newest historical payload.
    Returns a flat list of ``{name, ip}``; cross-device duplicates are expected
    (many agents print to one printer) and are deduped later in discovery.merge.
    """
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT h.payload AS hist_payload
            FROM devices d
            JOIN historical h ON h.device_id = d.device_id
              AND h.id = (SELECT MAX(id) FROM historical WHERE device_id = d.device_id)
            """
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        payload = json.loads(r["hist_payload"]) if r["hist_payload"] else {}
        # Read-side cap mirrors the contract max_length (shared.schema
        # PRINTER_PORTS_MAX): one bloated payload must not slow discovery.
        for p in (payload.get("printer_ports") or [])[:256]:
            if isinstance(p, dict) and p.get("ip"):
                out.append({"name": p.get("name"), "ip": p.get("ip")})
    return out


def get_recent_events(device_id: str, limit: int = 200) -> list[dict]:
    """Recent event rows (newest-first) for analytics that match on provider+id.

    Returns lightweight rows (no message body) so the disk-fill / servicing engine
    can filter WindowsUpdateClient failures by source rather than a bare numeric id.
    """
    with _connect() as conn:
        rows = conn.execute(
            """SELECT ts, received_at, source, event_id, level
               FROM events WHERE device_id=? ORDER BY id DESC LIMIT ?""",
            (device_id, limit),
        ).fetchall()
    return [
        {
            "ts": r["ts"],
            "received_at": r["received_at"],
            "source": r["source"],
            "event_id": r["event_id"],
            "level": r["level"],
        }
        for r in rows
    ]


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


def count_events_since(device_id: str, event_ids: list[int], since_iso: str) -> int:
    """Count matching events the server *received* at/after since_iso (W0.2).

    Burst/window detection must anchor on server receipt, not the client event
    timestamp, which depends on the machine's (possibly wrong) clock.
    """
    if not event_ids:
        return 0
    placeholders = ",".join("?" for _ in event_ids)
    with _connect() as conn:
        row = conn.execute(
            # B608: placeholders are only "?" marks; all values are parameterized.
            f"""SELECT COUNT(*) AS n FROM events
                WHERE device_id=? AND received_at >= ? AND event_id IN ({placeholders})""",  # nosec B608
            (device_id, since_iso, *event_ids),
        ).fetchone()
    return int(row["n"])


# --------------------------------------------------------------------------- #
# W4.2 fleet-anomaly helpers
# --------------------------------------------------------------------------- #
def get_fleet_cohort_stats(
    model: Optional[str],
    site_code: Optional[str],
) -> dict[str, Any]:
    """Fleet-level aggregates for the model cohort and the site.

    Returns a dict with:
      cohort_size          — devices sharing the same model that have historical data
      cohort_bsod_pct      — fraction with bugchecks_30d >= 1
      cohort_kp41_pct      — fraction with kernel_power_41_30d >= 2
      cohort_rsi_low_pct   — fraction with reliability_stability_index < 5.0
      site_size            — devices sharing the same site_code that have historical data
      site_kp41_pct        — fraction at site with kernel_power_41_30d >= 2

    All fractions are 0.0 when no devices with historical data exist in the group.
    Uses json_extract (SQLite 3.38+) to read fields from the historical payload blob.
    """
    with _connect() as conn:
        # Cohort stats: devices with the same model.
        if model:
            cohort_row = conn.execute(
                # B608: table and column names are literals; only model is a parameter.
                """
                SELECT
                    COUNT(*) AS cohort_size,
                    AVG(CASE WHEN CAST(json_extract(h.payload,'$.bugchecks_30d') AS REAL) >= 1
                             THEN 1.0 ELSE 0.0 END) AS bsod_pct,
                    AVG(CASE WHEN CAST(json_extract(h.payload,'$.kernel_power_41_30d') AS REAL) >= 2
                             THEN 1.0 ELSE 0.0 END) AS kp41_pct,
                    AVG(CASE
                        WHEN json_extract(h.payload,'$.reliability_stability_index')
                             IS NOT NULL
                         AND CAST(json_extract(
                               h.payload,'$.reliability_stability_index'
                             ) AS REAL) < 5.0
                        THEN 1.0 ELSE 0.0 END) AS rsi_low_pct
                FROM devices d
                JOIN (SELECT device_id, MAX(id) AS lid FROM historical GROUP BY device_id) lh
                  ON lh.device_id = d.device_id
                JOIN historical h ON h.id = lh.lid
                WHERE d.model = ?
                """,  # nosec B608
                (model,),
            ).fetchone()
        else:
            cohort_row = None

        # Site stats: devices with the same site_code.
        if site_code:
            site_row = conn.execute(
                # B608: same pattern — only site_code is a parameter.
                """
                SELECT
                    COUNT(*) AS site_size,
                    AVG(CASE WHEN CAST(json_extract(h.payload,'$.kernel_power_41_30d') AS REAL) >= 2
                             THEN 1.0 ELSE 0.0 END) AS kp41_pct
                FROM devices d
                JOIN (SELECT device_id, MAX(id) AS lid FROM historical GROUP BY device_id) lh
                  ON lh.device_id = d.device_id
                JOIN historical h ON h.id = lh.lid
                WHERE d.site_code = ?
                """,  # nosec B608
                (site_code,),
            ).fetchone()
        else:
            site_row = None

    return {
        "cohort_size": int(cohort_row["cohort_size"]) if cohort_row else 0,
        "cohort_bsod_pct": float(cohort_row["bsod_pct"] or 0.0) if cohort_row else 0.0,
        "cohort_kp41_pct": float(cohort_row["kp41_pct"] or 0.0) if cohort_row else 0.0,
        "cohort_rsi_low_pct": float(cohort_row["rsi_low_pct"] or 0.0) if cohort_row else 0.0,
        "site_size": int(site_row["site_size"]) if site_row else 0,
        "site_kp41_pct": float(site_row["kp41_pct"] or 0.0) if site_row else 0.0,
    }


def get_device_model_site(device_id: str) -> tuple[Optional[str], Optional[str]]:
    """Return (model, site_code) from the devices table for fleet-cohort keying."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT model, site_code FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
    if row is None:
        return None, None
    return row["model"], row["site_code"]


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


def upsert_source_trust(
    device_id: str,
    source: str,
    state: str,
    weight: float,
    collector_status: str,
    semantic_status: str,
    reason: str,
    ts: str,
) -> None:
    """Insert or replace the per-source trust row for a (device, source) pair."""
    with _lock, _connect() as conn:
        conn.execute(
            """
            INSERT INTO device_source_trust
              (device_id, source, state, weight, collector_status, semantic_status, reason, ts)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(device_id, source) DO UPDATE SET
              state            = excluded.state,
              weight           = excluded.weight,
              collector_status = excluded.collector_status,
              semantic_status  = excluded.semantic_status,
              reason           = excluded.reason,
              ts               = excluded.ts
            """,
            (device_id, source, state, weight, collector_status, semantic_status, reason, ts),
        )


def get_source_trusts(device_id: str) -> dict[str, dict]:
    """Return all per-source trust rows for a device as {source: row_dict}."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT source, state, weight, collector_status, semantic_status, reason, ts
            FROM device_source_trust
            WHERE device_id=?
            """,
            (device_id,),
        ).fetchall()
    return {
        r["source"]: {
            "source": r["source"],
            "state": r["state"],
            "weight": r["weight"],
            "collector_status": r["collector_status"],
            "semantic_status": r["semantic_status"],
            "reason": r["reason"],
            "ts": r["ts"],
        }
        for r in rows
    }


_METRIC_TABLES = ("devices", "heartbeats", "historical", "events", "scores")
# Table names are module constants (never user-supplied) — SQL injection not possible.
_GATE_FAIL_STATES = frozenset({"unavailable", "stale", "suspect"})
_GATE_PASS_STATES = frozenset({"ok", "degraded"})


def get_pipeline_metrics() -> dict[str, Any]:
    """Single-pass pipeline health stats for /api/v1/metrics and /pipeline page."""
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
        stale = conn.execute(
            "SELECT COUNT(*) FROM devices WHERE last_seen < datetime('now', ?)",
            (f"-{_STALE_AFTER_SEC} seconds",),
        ).fetchone()[0]

        score_row = conn.execute("SELECT COUNT(DISTINCT device_id), MAX(ts) FROM scores").fetchone()
        scored: int = score_row[0]
        newest_score_ts: Optional[str] = score_row[1]

        at_risk = conn.execute(
            """
            SELECT COUNT(*) FROM scores s
            JOIN (
              SELECT device_id, MAX(id) AS max_id FROM scores GROUP BY device_id
            ) m ON s.device_id = m.device_id AND s.id = m.max_id
            WHERE s.risk_exposure >= 50
            """
        ).fetchone()[0]

        # Ingest activity via server-stamped received_at (W0.2)
        hb_5m = conn.execute(
            "SELECT COUNT(*) FROM heartbeats WHERE received_at >= datetime('now', '-5 minutes')"
        ).fetchone()[0]
        hb_1h = conn.execute(
            "SELECT COUNT(*) FROM heartbeats WHERE received_at >= datetime('now', '-1 hour')"
        ).fetchone()[0]
        hist_5m = conn.execute(
            "SELECT COUNT(*) FROM historical WHERE received_at >= datetime('now', '-5 minutes')"
        ).fetchone()[0]
        hist_1h = conn.execute(
            "SELECT COUNT(*) FROM historical WHERE received_at >= datetime('now', '-1 hour')"
        ).fetchone()[0]

        # Source health breakdown from per-(device, source) trust table
        src_rows = conn.execute(
            "SELECT state, COUNT(*) FROM device_source_trust GROUP BY state"
        ).fetchall()

        # Lightweight row counts for storage awareness
        table_rows: dict[str, int] = {}
        for tbl in _METRIC_TABLES:
            table_rows[tbl] = conn.execute(
                f"SELECT COUNT(*) FROM {tbl}"  # nosec B608 — constant table name
            ).fetchone()[0]

    src_by_state: dict[str, int] = {r[0]: r[1] for r in src_rows}
    gate_pass = sum(src_by_state.get(s, 0) for s in _GATE_PASS_STATES)
    gate_fail = sum(src_by_state.get(s, 0) for s in _GATE_FAIL_STATES)
    not_applicable = src_by_state.get("not_applicable", 0)

    return {
        "ts": _now_iso(),
        "fleet": {
            "total": total,
            "stale": stale,
            "at_risk": at_risk,
            "scored": scored,
        },
        "ingest": {
            "heartbeats_5m": hb_5m,
            "heartbeats_1h": hb_1h,
            "historical_5m": hist_5m,
            "historical_1h": hist_1h,
        },
        "source_health": {
            "gate_pass": gate_pass,
            "gate_fail": gate_fail,
            "not_applicable": not_applicable,
        },
        "scores": {
            "newest_age_sec": _age_seconds(newest_score_ts),
            "newest_ts": newest_score_ts,
        },
        "table_rows": table_rows,
    }


# --------------------------------------------------------------------------- #
# Print tracking
# --------------------------------------------------------------------------- #
def store_print_jobs(
    device_id: str,
    jobs: list[dict[str, Any]],
    received_at: Optional[str] = None,
) -> int:
    """Insert print jobs; dedup via UNIQUE(device_id, job_id). Returns inserted count."""
    if not jobs:
        return 0
    recv = received_at or _now_iso()
    inserted = 0
    with _lock, _connect() as conn:
        for job in jobs:
            try:
                conn.execute(
                    """INSERT INTO print_jobs
                         (device_id, job_id, ts, received_at, printer, user_name, pages,
                          size_bytes, source)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        device_id,
                        job.get("job_id"),
                        job.get("ts"),
                        recv,
                        job.get("printer"),
                        job.get("user_name"),
                        job.get("pages"),
                        job.get("size_bytes"),
                        job.get("source"),
                    ),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                pass  # duplicate job_id for this device — already stored
    return inserted


def get_device_print(device_id: str, days: int = 30) -> dict[str, Any]:
    """Print stats for a single device over the last *days* days (0 = all time)."""
    ts_filter = f"AND ts >= datetime('now', '-{days} days')" if days > 0 else ""
    with _connect() as conn:
        total_row = conn.execute(
            f"SELECT COUNT(*) AS jobs, COALESCE(SUM(pages),0) AS pages"  # nosec B608
            f" FROM print_jobs WHERE device_id=? {ts_filter}",
            (device_id,),
        ).fetchone()
        printer_rows = conn.execute(
            f"SELECT printer, COALESCE(SUM(pages),0) AS pages, COUNT(*) AS jobs"  # nosec B608
            f" FROM print_jobs WHERE device_id=? {ts_filter}"
            " GROUP BY printer ORDER BY pages DESC",
            (device_id,),
        ).fetchall()
        daily_rows = conn.execute(
            f"SELECT strftime('%Y-%m-%d', ts) AS date,"  # nosec B608
            f" COALESCE(SUM(pages),0) AS pages, COUNT(*) AS jobs"
            f" FROM print_jobs WHERE device_id=? {ts_filter}"
            " GROUP BY date ORDER BY date",
            (device_id,),
        ).fetchall()
        recent_rows = conn.execute(
            f"SELECT ts, printer, pages, size_bytes"  # nosec B608
            f" FROM print_jobs WHERE device_id=? {ts_filter}"
            " ORDER BY ts DESC LIMIT 20",
            (device_id,),
        ).fetchall()
    return {
        "device_id": device_id,
        "period_days": days,
        "total_pages": total_row["pages"],
        "total_jobs": total_row["jobs"],
        "printers": [
            {"name": r["printer"], "pages": r["pages"], "jobs": r["jobs"]} for r in printer_rows
        ],
        "daily": [{"date": r["date"], "pages": r["pages"], "jobs": r["jobs"]} for r in daily_rows],
        "recent": [
            {
                "ts": r["ts"],
                "printer": r["printer"],
                "pages": r["pages"],
                "size_bytes": r["size_bytes"],
            }
            for r in recent_rows
        ],
    }


def _local_day_start_utc() -> str:
    """Start of the current local calendar day, expressed as a UTC ISO string.

    Print-job ``ts`` values are UTC ISO8601 (the agent stamps them with
    ``ToUniversalTime``). Comparing them lexically against this cutoff yields
    "printed today" in the server's local timezone (the office timezone),
    without depending on SQLite parsing the stored 'Z' suffix.
    """
    local_midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def get_fleet_print(days: int = 30, *, today: bool = False) -> dict[str, Any]:
    """Fleet-level print totals.

    ``today=True`` counts only the current local calendar day; otherwise the
    last *days* days (0 = all time).
    """
    params: tuple[Any, ...] = ()
    if today:
        params = (_local_day_start_utc(),)
        ts_f, pts_f = "AND ts >= ?", "AND p.ts >= ?"
    elif days > 0:
        ts_f = f"AND ts >= datetime('now', '-{days} days')"
        pts_f = f"AND p.ts >= datetime('now', '-{days} days')"
    else:
        ts_f = pts_f = ""
    with _connect() as conn:
        total_row = conn.execute(
            f"SELECT COALESCE(SUM(pages),0) AS pages, COUNT(*) AS jobs"  # nosec B608
            f" FROM print_jobs WHERE 1=1 {ts_f}",
            params,
        ).fetchone()
        device_rows = conn.execute(
            f"SELECT p.device_id, COALESCE(d.hostname, p.device_id) AS hostname,"  # nosec B608
            f" COALESCE(SUM(p.pages),0) AS pages, COUNT(*) AS jobs"
            f" FROM print_jobs p LEFT JOIN devices d ON d.device_id = p.device_id"
            f" WHERE 1=1 {pts_f}"
            " GROUP BY p.device_id ORDER BY pages DESC",
            params,
        ).fetchall()
        printer_rows = conn.execute(
            f"SELECT printer, COALESCE(SUM(pages),0) AS pages,"  # nosec B608
            f" COUNT(DISTINCT device_id) AS devices"
            f" FROM print_jobs WHERE 1=1 {ts_f}"
            " GROUP BY printer ORDER BY pages DESC",
            params,
        ).fetchall()
    return {
        "period_days": 0 if today else days,
        "today": today,
        "total_pages": int(total_row["pages"]),
        "total_jobs": int(total_row["jobs"]),
        "printer_count": len(printer_rows),
        "devices": [
            {
                "device_id": r["device_id"],
                "hostname": r["hostname"],
                "pages": r["pages"],
                "jobs": r["jobs"],
            }
            for r in device_rows
        ],
        "printers": [
            {"name": r["printer"], "pages": r["pages"], "devices": r["devices"]}
            for r in printer_rows
        ],
    }


def get_print_analytics(days: int = 30) -> dict[str, Any]:
    """All chart data for the /print analytics page (daily/printers/users/departments)."""
    ts_f = f"AND ts >= datetime('now', '-{days} days')" if days > 0 else ""
    pts_f = f"AND p.ts >= datetime('now', '-{days} days')" if days > 0 else ""
    with _connect() as conn:
        total_row = conn.execute(
            f"SELECT COALESCE(SUM(pages),0) AS pages, COUNT(*) AS jobs"  # nosec B608
            f" FROM print_jobs WHERE 1=1 {ts_f}",
            (),
        ).fetchone()
        total_pages = int(total_row["pages"])
        _denom = total_pages if total_pages > 0 else 1

        daily_rows = conn.execute(
            f"SELECT strftime('%Y-%m-%d', ts) AS date,"  # nosec B608
            f" COALESCE(SUM(pages),0) AS pages, COUNT(*) AS jobs"
            f" FROM print_jobs WHERE 1=1 {ts_f}"
            " GROUP BY date ORDER BY date",
            (),
        ).fetchall()
        printer_rows = conn.execute(
            f"SELECT printer, COALESCE(SUM(pages),0) AS pages, COUNT(*) AS jobs,"  # nosec B608
            f" COUNT(DISTINCT device_id) AS devices_count"
            f" FROM print_jobs WHERE 1=1 {ts_f}"
            " GROUP BY printer ORDER BY pages DESC",
            (),
        ).fetchall()
        user_rows = conn.execute(
            f"SELECT user_name, COALESCE(SUM(pages),0) AS pages, COUNT(*) AS jobs"  # nosec B608
            f" FROM print_jobs WHERE user_name IS NOT NULL AND user_name != '' {ts_f}"
            " GROUP BY user_name ORDER BY pages DESC LIMIT 20",
            (),
        ).fetchall()
        # Raw codes only -- names are decoded render-time from org_directory so
        # a rename reflects across all history without a rewrite (tray spec §7).
        dept_rows = conn.execute(
            f"SELECT d.org_code AS org_code, d.dept_code AS dept_code,"  # nosec B608
            f" d.department AS department,"
            f" COALESCE(SUM(p.pages),0) AS pages, COUNT(*) AS jobs,"
            f" COUNT(DISTINCT p.device_id) AS devices_count"
            f" FROM print_jobs p LEFT JOIN devices d ON d.device_id = p.device_id"
            f" WHERE 1=1 {pts_f}"
            " GROUP BY d.org_code, d.dept_code, d.department ORDER BY pages DESC",
            (),
        ).fetchall()
        if days > 0:
            prev_row = conn.execute(
                f"SELECT COALESCE(SUM(pages),0) AS pages, COUNT(*) AS jobs"  # nosec B608
                f" FROM print_jobs"
                f" WHERE ts >= datetime('now', '-{days * 2} days')"
                f" AND ts < datetime('now', '-{days} days')",
                (),
            ).fetchone()
            prev_pages = int(prev_row["pages"])
            prev_jobs = int(prev_row["jobs"])
        else:
            prev_pages = 0
            prev_jobs = 0

    return {
        "period_days": days,
        "total_pages": total_pages,
        "total_jobs": int(total_row["jobs"]),
        "prev_total_pages": prev_pages,
        "prev_total_jobs": prev_jobs,
        "daily": [
            {"date": r["date"], "pages": int(r["pages"]), "jobs": int(r["jobs"])}
            for r in daily_rows
        ],
        "printers": [
            {
                "name": r["printer"] or "(unknown)",
                "pages": int(r["pages"]),
                "jobs": int(r["jobs"]),
                "devices_count": int(r["devices_count"]),
                "pct": round(100.0 * r["pages"] / _denom, 1),
            }
            for r in printer_rows
        ],
        "users": [
            {
                "user_name": r["user_name"],
                "pages": int(r["pages"]),
                "jobs": int(r["jobs"]),
                "pct": round(100.0 * r["pages"] / _denom, 1),
            }
            for r in user_rows
        ],
        "departments": [
            {
                "org_code": r["org_code"],
                "dept_code": r["dept_code"],
                "department": r["department"],
                "pages": int(r["pages"]),
                "jobs": int(r["jobs"]),
                "devices_count": int(r["devices_count"]),
            }
            for r in dept_rows
        ],
    }


def export_print_rows(days: int = 30) -> list[dict[str, Any]]:
    """Raw print job rows for CSV export, enriched with hostname + department."""
    ts_f = f"AND p.ts >= datetime('now', '-{days} days')" if days > 0 else ""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT p.ts, p.device_id,"  # nosec B608
            f" COALESCE(d.hostname, p.device_id) AS hostname,"
            f" COALESCE(d.org_code, '') AS org_code,"
            f" COALESCE(d.dept_code, '') AS dept_code,"
            f" COALESCE(d.department, '') AS department,"
            f" COALESCE(p.printer, '') AS printer,"
            f" COALESCE(p.user_name, '') AS user_name,"
            f" p.pages, p.size_bytes,"
            f" COALESCE(p.source, '') AS source"
            f" FROM print_jobs p LEFT JOIN devices d ON d.device_id = p.device_id"
            f" WHERE 1=1 {ts_f}"
            " ORDER BY p.ts DESC",
            (),
        ).fetchall()
    return [dict(r) for r in rows]


def set_device_department(device_id: str, department: Optional[str]) -> bool:
    """Set the (deprecated) free-text department label. Returns True if the
    device existed. Superseded by dept_code + org_directory (tray spec §7);
    kept for the transition."""
    with _lock, _connect() as conn:
        n = conn.execute(
            "UPDATE devices SET department=? WHERE device_id=?",
            (department, device_id),
        ).rowcount
    return n > 0


def set_device_comment(device_id: str, comment: Optional[str]) -> bool:
    """Set the free-text machine comment. Returns True if the device existed."""
    with _lock, _connect() as conn:
        n = conn.execute(
            "UPDATE devices SET comment=? WHERE device_id=?",
            (comment, device_id),
        ).rowcount
    return n > 0
