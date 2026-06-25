"""REST API: ingest telemetry + query device state and scores."""

from __future__ import annotations

import csv
import hmac
import io
import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from shared.schema import Envelope, utcnow_iso

from server import db, org_directory
from server.analytics.diagnostics import compute_diagnostics
from server.ingest_guards import check_idempotency, check_rate_limit
from server.netdisco import reconcile as netdisco_reconcile
from server.netdisco import scheduler as netdisco_scheduler
from server.netdisco.cache import GraphCache
from server.netdisco.metrics import METRICS
from server.pipeline import ingest_envelope
from server.printers import scheduler

router = APIRouter(prefix="/api/v1")


@router.get("/health")
def health() -> dict:
    return {"status": "ok"}


@router.post("/ingest")
def ingest(env: Envelope, request: Request) -> dict:
    expected = getattr(request.app.state, "ingest_token", "")
    provided = request.headers.get("x-srp-token") or ""
    if expected and not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="invalid or missing ingest token")
    if not check_rate_limit(env.device_id):
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    if not check_idempotency(env.idempotency_key):
        return {"device_id": env.device_id, "msg_type": env.msg_type, "duplicate": True}
    try:
        return ingest_envelope(env)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/devices")
def list_devices() -> list[dict]:
    return db.get_devices()


@router.get("/devices/{device_id}")
def get_device(device_id: str) -> dict:
    device = db.get_device(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="device not found")
    return device


@router.get("/diagnostics/{device_id}")
def diagnostics(device_id: str) -> dict:
    """W4.1 trajectory: deterministic slopes + ETA + the trajectory_risk axis."""
    result = compute_diagnostics(device_id)
    if result is None:
        raise HTTPException(status_code=404, detail="device not found")
    return result


class AckBody(BaseModel):
    note: str = Field(default="", max_length=1000)


@router.post("/devices/{device_id}/ack")
def ack_device(device_id: str, body: AckBody) -> dict:
    """Operator feedback: acknowledge a device + attach a note."""
    if db.get_device(device_id) is None:
        raise HTTPException(status_code=404, detail="device not found")
    db.set_ack(device_id, body.note, utcnow_iso())
    return {"status": "ok"}


@router.get("/metrics")
def metrics() -> dict:
    """Pipeline health snapshot: fleet counts, ingest rate, source health, DB sizes."""
    return db.get_pipeline_metrics()


@router.get("/netmap")
def netmap(request: Request) -> dict:
    """DEPRECATED alias of ``/api/v1/network-map/graph`` (Ф3 unification).

    Returns the SAME unified superset graph (nodes/links/subnets/totals) -- not the
    legacy cluster model -- so every map consumer speaks one contract. The dedicated
    canvas (Ф4) and the panel (Ф5) read ``/api/v1/network-map/graph`` directly; this
    route is kept so older links/scripts keep working during the unification."""
    return _network_map_graph(request)


@router.get("/network-map/graph")
def network_map_graph(request: Request, at: Optional[str] = None) -> dict:
    """Ф3: the ONE unified network-map graph (netdisco backbone + Phase-1 identity FKs
    + netmap overlays), served from a short-TTL cache so a polling dashboard does not
    re-query the DB or re-run the assembler on every request.

    Ф5: ``?at=<snapshot_id>`` returns the HISTORICAL frame stored at that topology
    cycle instead. It bypasses the live cache (a historical frame must never poison
    it) and carries no live overlays (ICMP quality / subnet anomaly are derived per
    request and were never persisted -- D5: no false confidence in a stale frame)."""
    if at is not None and at != "":
        return _historical_map_graph(at)
    return _network_map_graph(request)


def _historical_map_graph(at: str) -> dict:
    """Ф5 time machine: one stored snapshot normalised into the unified shape (via the
    shared ``historical_graph_from_snapshot`` assembler helper, so the API and the SSR
    route stay byte-identical). It bypasses the live GraphCache (a historical frame must
    never poison it) and carries no live overlays (D5)."""
    try:
        sid = int(at)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="snapshot not found") from exc
    snap = db.get_topology_snapshot(sid)
    if snap is None:
        raise HTTPException(status_code=404, detail="snapshot not found")
    from server.netdisco.unified import historical_graph_from_snapshot

    return historical_graph_from_snapshot(snap)


@router.get("/network-map/snapshots")
def network_map_snapshots(limit: int = 200) -> dict:
    """Ф5 time machine: list of topology snapshots (newest first) for the slider.

    ``limit`` is clamped inside the reader (1..500). Ids/received_at/counts only -- the
    graph blob is fetched on demand via ``/network-map/graph?at=<id>``."""
    return {"snapshots": db.list_topology_snapshots(limit=limit)}


def _network_map_graph(request: Request) -> dict:
    # ``app.state.network_map_cache`` is created up-front in ``create_app`` (main.py);
    # the fallback only covers an app built outside ``create_app``.
    cache = getattr(request.app.state, "network_map_cache", None) or GraphCache()
    return cache.get() or {}


@router.get("/netdisco/devices")
def netdisco_devices(dev_type: Optional[str] = None, site: Optional[str] = None) -> dict:
    """Persistent network-device inventory (netdisco phase 3), optionally filtered
    by device type (router/switch/ap/agent/endpoint/unknown) and site."""
    return {"devices": db.get_net_devices(dev_type=dev_type, site=site)}


@router.post("/discovery/poll")
def poll_discovery(request: Request) -> dict:
    """Force one netdisco inventory cycle now (dashboard button). The endpoint is
    unauthenticated, so it is rate-limited (a single shared bucket) AND bounded by
    the scheduler's anti-DoS lock -- a concurrent call returns busy, not a second
    pass. This guard must stay ahead of P5's active scan sitting behind that lock."""
    # namespaced key: never collides with a real device_id in the shared limiter
    if not check_rate_limit("endpoint:discovery_poll"):
        raise HTTPException(status_code=429, detail="discovery poll rate exceeded")
    result = netdisco_scheduler.poll_now()
    # A fresh inventory can change the graph (new/retyped devices, FK identity links)
    # -- drop the unified-map cache so the next read rebuilds over the new backbone.
    _invalidate_network_map_cache(request)
    return result


@router.get("/topology/graph")
def topology_graph(request: Request) -> dict:
    """DEPRECATED alias of ``/api/v1/network-map/graph`` (Ф3 unification).

    Returns the SAME unified superset graph (the stored snapshot graph was a subset
    of it). Kept so existing topology links/scripts keep working during the
    unification."""
    return _network_map_graph(request)


@router.post("/topology/poll")
def poll_topology(request: Request) -> dict:
    """Force one topology reconcile now (dashboard "собрать топологию сейчас" button):
    probe the known infra for L2 evidence (LLDP/CDP/FDB), fuse it, and rebuild the
    persistent graph from the data on hand -- after which the background loop keeps
    it fresh. Unauthenticated, so it is rate-limited AND serialized by the scheduler's
    anti-DoS lock (a concurrent cycle returns ``busy`` instead of a second pass). Only
    RFC1918 infra is ever probed and SNMP stays read-only."""
    if not check_rate_limit("endpoint:topology_poll"):
        raise HTTPException(status_code=429, detail="topology poll rate exceeded")
    cfg = getattr(request.app.state, "netdisco_config", None)
    if cfg is None:
        from server.netdisco.config import load_netdisco_config

        cfg = load_netdisco_config(None)
    result = netdisco_reconcile.run_topology_cycle(cfg)
    # The reconcile may have written fresh net_links/snapshots -> drop the unified-map
    # cache so the next read reflects the new edges at once (not after the TTL lapses).
    _invalidate_network_map_cache(request)
    return result


def _invalidate_network_map_cache(request: Request) -> None:
    """Drop the unified-map cache if one was lazily created on this app."""
    cache = getattr(request.app.state, "network_map_cache", None)
    if cache is not None:
        cache.invalidate()


@router.get("/topology/changes")
def topology_changes(days: int = 7) -> dict:
    """Topology-change journal for the last *days* (clamped 1..365), newest first."""
    clamped_days = max(1, min(days, 365))
    return {"changes": db.get_net_changes(days=clamped_days, limit=2000)}


@router.get("/netdisco/devices/{device_nid}")
def netdisco_device(device_nid: str) -> dict:
    """One network device: interfaces, links and current reachability status
    (up/down/unreachable/missing) -- the read-side correlation annotation."""
    dev = db.get_net_device(device_nid)
    if dev is None:
        raise HTTPException(status_code=404, detail="device not found")
    return {"device": dev}


@router.get("/netdisco/stats")
def netdisco_stats() -> dict:
    """Scanner telemetry counters (cycles, probes, links, deltas, outages found)."""
    return {"stats": METRICS.snapshot()}


# ---------------------------------------------------------------------------
# Print tracking endpoints
# ---------------------------------------------------------------------------

_DAYS_MIN = 0
_DAYS_MAX = 365


def _clamp_days(days: int) -> int:
    return min(max(days, _DAYS_MIN), _DAYS_MAX)


_FILTER_MAX_LEN = 256


def _q(value: Optional[str]) -> Optional[str]:
    """Normalize a query-string filter: strip, length-cap, empty -> None."""
    if not value:
        return None
    capped = value.strip()[:_FILTER_MAX_LEN]
    return capped or None


def _print_filter(
    date_from: Optional[str],
    date_to: Optional[str],
    device: Optional[str],
    printer: Optional[str],
    ip: Optional[str],
) -> db.PrintFilter:
    """Build a PrintFilter from raw query params (normalized; empty -> unfiltered)."""
    return db.PrintFilter(
        date_from=_q(date_from),
        date_to=_q(date_to),
        device=_q(device),
        printer=_q(printer),
        ip=_q(ip),
    )


@router.get("/devices/{device_id}/print")
def device_print(device_id: str, days: int = 30) -> dict:
    if db.get_device(device_id) is None:
        raise HTTPException(status_code=404, detail="device not found")
    return db.get_device_print(device_id, days=_clamp_days(days))


def _decode_departments(raw: list[dict]) -> list[dict]:
    """Decode raw (org_code, dept_code, department) buckets to display labels
    and merge buckets that render to the same label (tray spec §7).

    Each device maps to exactly one raw bucket, so summing ``devices_count``
    across merged buckets stays correct. ``known=False`` carries the
    "not in directory" chip through to the chart.
    """
    directory = org_directory.get_directory()
    merged: dict[tuple, dict] = {}
    for row in raw:
        label = directory.dept_display(
            row.get("org_code"), row.get("dept_code"), row.get("department")
        )
        key = (label.text, label.known)
        bucket = merged.setdefault(
            key,
            {"dept": label.text, "known": label.known, "pages": 0, "jobs": 0, "devices_count": 0},
        )
        bucket["pages"] += int(row.get("pages") or 0)
        bucket["jobs"] += int(row.get("jobs") or 0)
        bucket["devices_count"] += int(row.get("devices_count") or 0)
    return sorted(merged.values(), key=lambda b: b["pages"], reverse=True)


@router.get("/fleet/print/analytics")
def fleet_print_analytics(
    days: int = 30,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict:
    """Legacy chart sections. An explicit date_from/date_to range overrides the
    last-*days* window so every section reacts to the shared printview range."""
    data = db.get_print_analytics(
        days=_clamp_days(days), date_from=_q(date_from), date_to=_q(date_to)
    )
    data["departments"] = _decode_departments(data["departments"])
    return data


@router.get("/fleet/print/summary")
def fleet_print_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    device: Optional[str] = None,
    printer: Optional[str] = None,
    ip: Optional[str] = None,
) -> dict:
    """Headline print metrics (7 summary cards) honoring the print filters."""
    return db.get_print_summary(_print_filter(date_from, date_to, device, printer, ip))


@router.get("/fleet/print/series")
def fleet_print_series(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    device: Optional[str] = None,
    printer: Optional[str] = None,
    ip: Optional[str] = None,
    granularity: str = "auto",
    max_series: int = 12,
) -> dict:
    """Hero-chart time-series: (computer -> printer) pairs with auto bucket detail."""
    return db.get_print_series(
        _print_filter(date_from, date_to, device, printer, ip),
        granularity=_q(granularity) or "auto",
        max_series=max_series,
    )


# Operator-facing labels for a print row's data source. Machine value ``source``
# (events/counter) stays English; these are the Russian prose + chip color the
# dashboard renders. events = exact (journal); counter = estimate (spooler delta).
_PRINT_SOURCE_RU = {"events": "журнал", "counter": "счётчик"}
_PRINT_VALIDATION = {
    "events": ("точно", "good"),
    "counter": ("оценка", "warn"),
}


def _label_print_row(row: dict) -> dict:
    """Add localized source/validation labels to a records row (immutably)."""
    source = row.get("source") or ""
    valid, color = _PRINT_VALIDATION.get(source, ("—", "na"))
    return {
        **row,
        "source_label": _PRINT_SOURCE_RU.get(source, "—"),
        "validation": valid,
        "validation_color": color,
    }


@router.get("/fleet/print/records")
def fleet_print_records(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    device: Optional[str] = None,
    printer: Optional[str] = None,
    ip: Optional[str] = None,
    page: int = 1,
    page_size: int = 50,
    sort: str = "ts",
    dir: str = "desc",
    q: Optional[str] = None,
) -> dict:
    """One page of detailed print rows (events table) honoring the filters."""
    data = db.get_print_records(
        _print_filter(date_from, date_to, device, printer, ip),
        page=page,
        page_size=page_size,
        sort=_q(sort) or "ts",
        direction=dir,
        q=_q(q),
    )
    data["rows"] = [_label_print_row(r) for r in data["rows"]]
    return data


@router.get("/fleet/print/filter-options")
def fleet_print_filter_options(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> dict:
    """Distinct devices/printers/ips for the filter selects, scoped to the period."""
    return db.get_print_filter_options(_q(date_from), _q(date_to))


_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: object) -> object:
    """Defang spreadsheet formula injection: a string cell starting with one of
    = + - @ (or a tab/CR) gets a leading single quote so Excel/Sheets treat it as
    text, never a formula. Non-strings pass through untouched."""
    if isinstance(value, str) and value and value[0] in _CSV_FORMULA_PREFIXES:
        return "'" + value
    return value


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _filename_date(value: Optional[str]) -> str:
    """Compact YYYYMMDD if *value* is a well-formed ISO date, else 'all'. Strict
    validation here keeps any stray chars (newline/semicolon) out of the
    Content-Disposition header -- defense against response header injection."""
    if value and _ISO_DATE_RE.match(value):
        return value.replace("-", "")
    return "all"


def _export_filename(f: db.PrintFilter) -> str:
    """CSV filename reflecting the period (print_export_FROM_TO.csv; 'all' if open)."""
    return f"print_export_{_filename_date(f.date_from)}_{_filename_date(f.date_to)}.csv"


@router.get("/fleet/print/export.csv")
def fleet_print_export(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    device: Optional[str] = None,
    printer: Optional[str] = None,
    ip: Optional[str] = None,
) -> StreamingResponse:
    f = _print_filter(date_from, date_to, device, printer, ip)
    rows = db.export_print_rows(f)
    directory = org_directory.get_directory()
    out: list[dict] = []
    for row in rows:
        # Decode names render-time; the legacy free-text `department` column is
        # kept for the transition (tray spec §7). validation = RU label from source.
        row["org_name"] = directory.org_display(row.get("org_code")).text
        row["dept_name"] = directory.dept_display(
            row.get("org_code"), row.get("dept_code"), row.get("department")
        ).text
        row["validation"] = _PRINT_VALIDATION.get(row.get("source") or "", ("—", "na"))[0]
        out.append({k: _csv_safe(v) for k, v in row.items()})
    buf = io.StringIO()
    fieldnames = [
        "ts",
        "device_id",
        "hostname",
        "org_name",
        "dept_name",
        "printer",
        "ip",
        "pages",
        "size_bytes",
        "user_name",
        "department",
        "source",
        "validation",
    ]
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(out)
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={_export_filename(f)}"},
    )


@router.get("/fleet/print")
def fleet_print(days: int = 30, today: bool = False) -> dict:
    if today:
        return db.get_fleet_print(today=True)
    return db.get_fleet_print(days=_clamp_days(days))


class MetaPatch(BaseModel):
    # `department` is DEPRECATED (superseded by dept_code + org_directory, tray
    # spec §7) but still accepted for the transition. `comment` is the device's
    # free-text label going forward.
    department: Optional[str] = Field(default=None, max_length=200)
    comment: Optional[str] = Field(default=None, max_length=200)


@router.patch("/devices/{device_id}/meta")
def patch_device_meta(device_id: str, body: MetaPatch) -> dict:
    if db.get_device(device_id) is None:
        raise HTTPException(status_code=404, detail="device not found")
    if body.department is not None:
        db.set_device_department(device_id, body.department)  # deprecated path
    if body.comment is not None:
        db.set_device_comment(device_id, body.comment)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Device cleanup (ghost hygiene, 2026-06-16)
# ---------------------------------------------------------------------------
class PurgeBody(BaseModel):
    # Delete devices silent for at least this many days (server-stamped last_seen).
    # Minimum 1 so a stray days=0 can never wipe the whole fleet.
    days: int = Field(default=30, ge=1, le=3650)
    dry_run: bool = False


@router.post("/devices/{device_id}/delete")
def delete_device(device_id: str) -> dict:
    """Remove a device and ALL its data. POST-only so a stray GET never deletes."""
    if not db.delete_device(device_id):
        raise HTTPException(status_code=404, detail="device not found")
    return {"status": "ok", "deleted": True}


@router.post("/devices/purge")
def purge_devices(body: PurgeBody) -> dict:
    """Bulk-clear ghosts: delete (or, with dry_run, preview) devices silent past *days*."""
    return db.purge_devices_silent_for(body.days, dry_run=body.dry_run)


# ---------------------------------------------------------------------------
# Network printers (phase 6)
# ---------------------------------------------------------------------------
@router.get("/printers")
def list_printers(days: int = 30) -> dict:
    """Hardware printer inventory + per-printer software print reconcile."""
    return db.get_printers_overview(days=_clamp_days(days))


@router.get("/printers/{printer_id}")
def printer_detail(printer_id: str, days: int = 30) -> dict:
    """One printer: inventory + counter series + which PCs printed to it."""
    p = db.get_printer_detail(printer_id, days=_clamp_days(days))
    if p is None:
        raise HTTPException(status_code=404, detail="printer not found")
    return p


@router.post("/printers/poll")
def poll_printers(request: Request) -> dict:
    """Force one printer poll cycle now (dashboard button). Bounded by SNMP/HTTP
    timeouts; probes only already-discovered hosts, never scans ranges here."""
    printer_cfg = getattr(request.app.state, "printer_config", None)
    if printer_cfg is None:
        from server.printers.config import load_printer_config

        printer_cfg = load_printer_config(None)
    return scheduler.poll_now(printer_cfg)
