# Spec — Device-ghost cleanup (server-only DB hygiene)

_Status: APPROVED (user-directed 2026-06-16). Scope: server-only. Agent + contract UNTOUCHED._

## Problem
`device_id` is the server PRIMARY KEY. Fix `14c6a97` made it a **random per-install id**
(`sha256(MachineGuid + hostname + uuid4 nonce)`), guaranteeing uniqueness but making every
reinstall / config-wipe / clone-reset mint a **new** id. Consequences observed by the operator:

1. The machine's *old* row (old id) is orphaned and **lives in the `devices` table forever** —
   it shows in the dashboard as a ghost that will never update again.
2. `get_devices()` returns **every** row with **no freshness filter** (only a derived 15-min
   `stale` flag that still renders). There is **no delete/retire/archive** function anywhere.
3. So garbage accumulates with every reinstall and clutters the live view.

## Decisions (user, 2026-06-16)
- **No identity continuity.** History across reinstall is NOT required → no hardware fingerprint,
  no new collector, **no contract change, no CONTRACT_VERSION bump**. The random-nonce id stays.
- **Clones are physical PCs from one image** (each has its own disk) — no real-world collisions.
- **Ghosts: delete completely** (not hide).
- **No history merge** — a reinstalled machine starts fresh; its old row is deleted.
- **Deletion access: no auth** (dashboard is unauthenticated on a trusted LAN by design;
  external boundary stays `ingest_token`). Mitigate accidental deletes with **POST + explicit
  confirmation**, never GET.
- **Auto-delete after 30 days** of silence (`device_retention_days`, configurable; `0` = off).

## Design

### D1 — `delete_device(device_id)` (db.py)
One transaction, deletes the device's rows from **all** per-device tables. The table list is a
single module constant `_DEVICE_TABLES` (single source of truth) — omitting one would leave
orphan shards = a *new* kind of garbage. All SQL parameterized.

`_DEVICE_TABLES` (every table with a `device_id` column, verified against the live schema):
`devices, inventory, historical, heartbeats, events, scores, source_last_good, trust,
acknowledgements, device_source_trust, print_jobs`.

**Safety net:** a test introspects `sqlite_master` + `PRAGMA table_info` and asserts every table
that has a `device_id` column is present in `_DEVICE_TABLES`. A future per-device table that
forgets to register here will fail the test.

### D2 — `purge_devices_silent_for(days, *, dry_run=False)` (db.py)
Selects devices whose **server-stamped** `last_seen` is older than `now - days` (we never trust
the client clock; `last_seen` was re-anchored to `received_at` in W0.2). `dry_run=True` returns
the candidate list/count for a **preview** (used by the dashboard "purge offline" action and by
logging) without deleting. Reuses `delete_device` per candidate inside one transaction.

### D3 — Retention sweep (main.py lifespan + config)
`server/config.py`: add `device_retention_days: int = 30` (0 disables) and
`purge_interval_hours: int = 24`. On startup run one sweep, then a lightweight stdlib asyncio
background task repeats every `purge_interval_hours`, cancelled cleanly on shutdown. Every
deletion is logged (count + ids). No third-party scheduler (dep-averse).

### D4 — Dashboard shows only live machines (dashboard.py + templates)
- Fleet default view = devices seen within the retention window (offline ghosts excluded).
  Offline devices shown only via an explicit toggle / collapsed section, with a count.
- Per-device **"удалить запись"** button → `POST /api/v1/devices/{id}/delete` (confirmation in UI).
- **"Убрать офлайн > N дней"** action → `POST` preview (count) then `POST` purge. Lets the
  operator clear the *current* mess immediately, including freshly-orphaned ghosts that have not
  yet aged past the auto-threshold.
- Endpoints are POST-only (no GET delete → no browser prefetch / link-preview accidents).

### D5 — Agent / contract: NO CHANGE
`client/` and `shared/schema.py` are not touched. This is pure server DB hygiene.

## Edge cases
- **Race (delete vs reappear):** SQLite is single-writer; candidate selection + deletes run in one
  transaction filtered by `last_seen < cutoff`. A device that re-ingests after deletion simply
  re-creates its row via `upsert/touch_device` and starts fresh (acceptable per "no merge").
- **Temporarily-offline live machine:** keeps its persisted `device_id`; only deleted after the
  full retention window. Operator can raise `device_retention_days`.
- **Never delete inside the active window** — the cutoff guarantees it.

## Out of scope (explicitly)
Hardware fingerprinting, server-side identity reconciliation, history merge, updatable/canonical
id hand-back, dashboard authentication. (Parked; revisit only if continuity is later required.)

## Test plan (TDD)
1. `delete_device` removes the target's rows from all 11 tables and leaves a sibling device intact.
2. `_DEVICE_TABLES` schema-introspection guard (catches future omissions).
3. `purge_devices_silent_for`: deletes a >N-days-silent device, keeps a fresh one; `dry_run`
   deletes nothing and returns the correct candidates.
4. Race: a device that re-ingests immediately after delete reappears cleanly (no shards).
5. Dashboard: a just-connected agent shows full info; an offline ghost is excluded from the
   default fleet list; delete endpoint is POST-only and removes the row.
6. Gate: ruff/mypy/bandit + pytest cov ≥80% + smoke. Then security-review (SQL/ingest §3).
