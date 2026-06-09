# Print Tracking — Design Spec
_Date: 2026-06-09_

## Goal
Track pages printed on each Windows machine: count, printer, date/time, job size.
No document names. Physical printers only. Full history kept forever. Offline-safe.

---

## Decisions

| Decision | Choice | Reason |
|---|---|---|
| Paper format | Not collected | Not needed; Event 307 doesn't carry it anyway |
| Document name | Not stored | Privacy |
| Virtual printers | Filtered out | Only physical output matters |
| Collection source | Event ID 307, PrintService/Operational | Reliable, historical, stdlib-accessible |
| Offline safety | Existing `buffer.jsonl` transport | Zero new code — all msg_types already buffered |
| Retention | Unlimited (no pruning) | User requirement: store everything forever |

---

## Data Collected Per Job (client → server)

| Field | Source (Event 307) | Notes |
|---|---|---|
| `job_id` | Param2 | int; dedup key |
| `ts` | `TimeCreated` UTC | ISO8601 |
| `printer` | Param5 | physical printer name |
| `pages` | Param8 | int |
| `size_bytes` | Param7 | int |
| `user_name` | Param3 | operator identity |

Virtual printer filter (case-insensitive substring): `pdf`, `xps`, `fax`, `onenote`,
`microsoft print to`, `send to`, `adobe`, `docuworks`.

---

## Transport: msg_type = "print_jobs"

Payload:
```json
{
  "jobs": [
    { "job_id": 412, "ts": "2026-06-09T08:12:00Z",
      "printer": "HP LaserJet 1320", "pages": 3,
      "size_bytes": 45678, "user_name": "ivanov" }
  ],
  "window_from": "2026-06-09T07:00:00Z"
}
```

- New config field: `print_interval_sec` (default 900 = 15 min)
- State file: `print_state.json` next to `buffer.jsonl` — stores `last_sweep_ts`
- Each sweep: `Get-WinEvent` with `StartTime = last_sweep_ts`
- Offline: goes into `buffer.jsonl` unchanged; flushed on reconnect (existing transport)

---

## Database Schema

```sql
CREATE TABLE print_jobs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id   TEXT NOT NULL,
  job_id      INTEGER,           -- Windows spooler job id (NULL for legacy rows)
  ts          TEXT NOT NULL,     -- client print time (ISO8601 UTC)
  received_at TEXT,              -- server receipt time
  printer     TEXT,
  user_name   TEXT,
  pages       INTEGER,
  size_bytes  INTEGER
  -- NOTE: copies absent — Event 307 pages = total pages sent (already includes copies)
);
CREATE INDEX idx_print_device_ts ON print_jobs(device_id, ts);
-- Server-side dedup: same device + same Windows job_id = same job
CREATE UNIQUE INDEX idx_print_dedup ON print_jobs(device_id, job_id)
  WHERE job_id IS NOT NULL;
```

**No retention pruning.** All records kept indefinitely.

---

## API Endpoints

### `GET /api/v1/devices/{id}/print?days=30`
Returns:
```json
{
  "device_id": "...",
  "period_days": 30,
  "total_pages": 412,
  "total_jobs": 87,
  "printers": [{"name": "HP LaserJet 1320", "pages": 380, "jobs": 82}],
  "daily": [{"date": "2026-06-09", "pages": 15, "jobs": 3}],
  "recent": [{"ts": "...", "printer": "...", "pages": 3, "size_bytes": 45678}]
}
```

### `GET /api/v1/fleet/print?days=30`
Returns:
```json
{
  "period_days": 30,
  "total_pages": 12400,
  "devices": [
    {"device_id": "...", "hostname": "...", "pages": 412, "jobs": 87}
  ],
  "printers": [{"name": "HP LaserJet 1320", "pages": 8200, "devices": 12}]
}
```

---

## Dashboard Changes

**Device page** (`device.html`): new «Печать» block below existing sections.
- Summary: total pages / jobs for 7d and 30d
- Printer breakdown table
- Last 20 jobs table (ts, printer, pages, size_bytes)

**Fleet page** (`fleet.html`): new «Печать флота» block.
- Top devices by pages (last 30d)
- Top printers by pages (last 30d)
- Fleet total

---

## Shared Schema (shared/schema.py)

```python
class PrintJobRecord(_Base):
    job_id: Optional[int] = None
    ts: str
    printer: str
    pages: int
    size_bytes: Optional[int] = None
    user_name: Optional[str] = None

class PrintJobsPayload(_Base):
    jobs: list[PrintJobRecord] = Field(default_factory=list)
    window_from: Optional[str] = None
```

---

## Implementation Phases

1. **Schema** — `PrintJobRecord` + `PrintJobsPayload` in `shared/schema.py`
2. **DB** — `print_jobs` table, indexes, `store_print_jobs()`, `get_device_print()`, `get_fleet_print()`
3. **Config** — `print_interval_sec` in `ClientConfig`
4. **Collector** — `client/collectors/print_jobs.py` (PowerShell Event 307, state file, virtual filter)
5. **Agent loop** — wire print collector into `client/agent.py`
6. **Pipeline** — handle `print_jobs` msg_type in `server/pipeline.py`
7. **API** — two endpoints in `server/web/dashboard.py`
8. **Templates** — print blocks in `device.html` + `fleet.html`
9. **Tests** — collector parser, DB store/dedup, pipeline route, API responses

---

## Test Plan

- `test_print_collector.py`: virtual-printer filter, PowerShell output parsing, state-file tracking
- `test_print_db.py`: store, dedup (same job_id idempotent), query aggregates
- `test_print_pipeline.py`: integration — envelope → DB → API response
- Dashboard: visual check in browser
