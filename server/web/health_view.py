"""ssd3 Ф7 T7.1 -- fleet-wide ``/health`` triage page: route + data-assembly.

Split out of ``server/web/dashboard.py`` (which owns every other dashboard page)
purely to keep that file under this repo's 800-line cap -- this module is
otherwise just Task 1's code, unchanged. Everything here is Ф7-page-scoped: the
route handler and the KPI/heatmap/escalations/risk-models/worsening helpers it
calls. ``band_class`` stays in ``dashboard.py`` -- it is a general Ф6
band->CSS-class Jinja global shared with T7.2 (device hero), not scoped to this
page alone.

Registration: this module owns its own ``APIRouter`` and is merged into
``dashboard.router`` (see the ``router.include_router(...)`` call there), so
``server/main.py`` needs no changes -- it still only imports ``dashboard.router``.
The route handler imports ``server.web.dashboard``'s shared ``_TEMPLATES`` locally
(inside the function, not at module level) specifically to avoid a circular
import: ``dashboard.py`` imports THIS module's ``router`` at its own module-load
time, so this module must not import anything from ``dashboard`` until request
time, by which point both modules are already fully loaded.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from server import db
from server.analytics import health

router = APIRouter()

# --------------------------------------------------------------------------- #
# Data assembly (KPI/heatmap/escalations/risk-models). Pure functions over
# get_fleet_health() / _deltas() rows -- no I/O, unit-testable.
# --------------------------------------------------------------------------- #
_HEATMAP_STATE_PRIORITY = {"h4": 0, "h3": 1, "h2": 2, "h1": 3, "h0": 4}  # unknown sorts last (5)
_HEATMAP_ROW_CAP = 100
_HEATMAP_AXES = (
    ("storage", "Здоровье диска (SMART)"),
    ("aging", "Старение ПО"),
    ("os", "Стабильность ОС"),
    ("battery", "Здоровье батареи"),
    ("disk_fill", "Заполнение диска / обслуживание Windows"),
    ("network", "Здоровье сети"),
    ("trajectory", "Риск траектории"),
)
_HEATMAP_COLS = ["Состояние", "Повреждения (D)", "Устойчивость (R)", "Видимость (O)"] + [
    label for _, label in _HEATMAP_AXES
]
_BAND_ORDINAL = {"good": 0, "watch": 1, "bad": 2, "unknown": 3}
_SPARK_W = 100.0
_SPARK_H = 24.0


def _band_ord(band: Optional[str]) -> int:
    return _BAND_ORDINAL.get(band or "unknown", 3)


def _kpi_counts(rows: list[dict], deltas: list[dict], now: datetime) -> dict:
    """The 4 KPI-tile numbers: critical (h4), worsened, low-observability, stale."""
    critical = sum(1 for r in rows if r.get("state") == "h4")
    low_obs = sum(
        1 for r in rows if r.get("observability_pct") is not None and r["observability_pct"] < 40
    )
    stale = sum(
        1
        for r in rows
        if r.get("score_ts") and health.health_staleness(r["score_ts"], now) is not None
    )
    return {"critical": critical, "worsened": len(deltas), "low_obs": low_obs, "stale": stale}


_STATE_DIST_ORDER = ("h4", "h3", "h2", "h1", "h0", "unknown")
_BAND_SEVERITY = {"bad": 2, "watch": 1, "good": 0}  # higher = worse; "unknown" = no vote


def _state_distribution(rows: list[dict]) -> list[dict]:
    """Fleet counts + worst-actual-band per state (h0..h4 + unknown; unrecognised/
    None state -> "unknown"). Colour = the WORST ``band`` actually present in that
    bucket -- never guessed from ``state`` (``_reconcile`` clamps no band by state,
    so e.g. an h1 device can legitimately be band="good")."""
    counts = dict.fromkeys(_STATE_DIST_ORDER, 0)
    worst_band: dict[str, str] = dict.fromkeys(_STATE_DIST_ORDER, "unknown")
    for r in rows:
        state = r.get("state")
        key = state if state in counts else "unknown"
        counts[key] += 1
        band = r.get("band") or "unknown"
        if _BAND_SEVERITY.get(band, -1) > _BAND_SEVERITY.get(worst_band[key], -1):
            worst_band[key] = band
    return [
        {
            "state": s,
            "label": health.state_label_for(s),
            "count": counts[s],
            "band": worst_band[s],
        }
        for s in _STATE_DIST_ORDER
    ]


def _heatmap(rows: list[dict]) -> dict:
    """Device x dimension grid: rows sorted worst-state-first, index asc tiebreak,
    capped at 100. z is a discrete 0..3 band ordinal in EVERY column -- including
    "Состояние", which colours by the row's overall ``band`` (not the 5-value state
    rank) so the whole grid is uniformly "darker = worse" (ssd3 Ф7 T7.1)."""
    ordered = sorted(
        rows,
        key=lambda r: (
            _HEATMAP_STATE_PRIORITY.get(r.get("state") or "unknown", 5),
            r.get("index") if r.get("index") is not None else 1e9,
        ),
    )[:_HEATMAP_ROW_CAP]
    device_ids, hostnames, state_labels, dominant_labels, z = [], [], [], [], []
    for r in ordered:
        device_ids.append(r["device_id"])
        hostnames.append(r.get("hostname") or "")
        state_labels.append(health.state_label_for(r.get("state")))
        dominant_labels.append(health.dominant_label_for(r.get("dominant")))
        axis_bands = r.get("axis_bands") or {}
        row_z = [
            _band_ord(r.get("band")),
            _band_ord(r.get("damage_band")),
            _band_ord(r.get("resilience_band")),
            _band_ord(r.get("observability_band")),
        ]
        row_z.extend(_band_ord(axis_bands.get(key)) for key, _ in _HEATMAP_AXES)
        z.append(row_z)
    return {
        "device_ids": device_ids,
        "hostnames": hostnames,
        "state_labels": state_labels,
        "dominant_labels": dominant_labels,
        "cols": _HEATMAP_COLS,
        "z": z,
    }


def _escalations(deltas: list[dict], health_by_id: dict[str, dict]) -> list[dict]:
    """Join deltas against a {device_id: row} map from the SAME get_fleet_health()
    call (no extra query) -- adds dominant mechanism + Russian recommendation."""
    out = []
    for d in deltas:
        dominant = (health_by_id.get(d["device_id"]) or {}).get("dominant")
        out.append(
            {
                "device_id": d["device_id"],
                "hostname": d.get("hostname") or "",
                "prev_state": d.get("prev_state"),
                "prev_label": health.state_label_for(d.get("prev_state")),
                "state": d.get("state"),
                "state_label": health.state_label_for(d.get("state")),
                "dominant": dominant,
                "dominant_label": health.dominant_label_for(dominant),
                "action": health.action_for(dominant),
            }
        )
    return out


def _field_means(
    rows: list[dict], model_by_id: dict[str, Optional[str]], field: str
) -> dict[str, tuple[float, int]]:
    """(mean, count) of *field* per model, skipping rows where THAT field is None --
    called once per field so each coordinate's mean is independent (one device's gap
    in one field never drags another field's average toward 0)."""
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for r in rows:
        model = model_by_id.get(r["device_id"])
        v = r.get(field)
        if not model or v is None:
            continue
        sums[model] = sums.get(model, 0.0) + v
        counts[model] = counts.get(model, 0) + 1
    return {m: (round(sums[m] / counts[m], 1), counts[m]) for m in sums}


def _risk_models(rows: list[dict], model_by_id: dict[str, Optional[str]]) -> list[dict]:
    """Top-3 models by mean index (ascending -- lower = worse). K1: the projection
    never appears alone -- mean Damage/Resilience/Observability ride alongside it,
    each independently skipping devices missing that one field (_field_means)."""
    idx = _field_means(rows, model_by_id, "index")
    dmg = _field_means(rows, model_by_id, "damage")
    res = _field_means(rows, model_by_id, "resilience")
    obs = _field_means(rows, model_by_id, "observability_pct")
    ranked_models = sorted(idx, key=lambda m: idx[m][0])[:3]
    return [
        {
            "model": m,
            "mean_index": idx[m][0],
            "count": idx[m][1],
            "mean_damage": dmg[m][0] if m in dmg else None,
            "mean_resilience": res[m][0] if m in res else None,
            "mean_observability": obs[m][0] if m in obs else None,
        }
        for m in ranked_models
    ]


def _worsening_selection(rows: list[dict], limit: int = 10) -> list[dict]:
    """Top-N devices by delta_7d, most-negative (worsened most) first."""
    candidates = [r for r in rows if r.get("delta_7d") is not None and r["delta_7d"] < 0]
    return sorted(candidates, key=lambda r: r["delta_7d"])[:limit]


def _index_sparkline(series: list[dict]) -> dict:
    """Oldest->newest index polyline points ("x,y x,y ..." SVG string) for one device.
    Pre-Ф6 rows (no "health" key) are skipped -- a gap, never a fake index=0."""
    values: list[float] = []
    for row in reversed(series):  # series is newest-first; plot oldest -> newest
        health_blob = (row.get("risk") or {}).get("health")
        if isinstance(health_blob, dict) and health_blob.get("index") is not None:
            values.append(float(health_blob["index"]))
    n = len(values)
    if n == 0:
        return {"points": "", "count": 0}
    step = _SPARK_W / (n - 1) if n > 1 else 0.0
    pts = []
    for i, v in enumerate(values):
        x = i * step if n > 1 else _SPARK_W / 2
        y = _SPARK_H - (max(0.0, min(100.0, v)) / 100.0) * _SPARK_H
        pts.append(f"{x:.1f},{y:.1f}")
    return {"points": " ".join(pts), "count": n}


# --------------------------------------------------------------------------- #
# Route
# --------------------------------------------------------------------------- #
@router.get("/health", response_class=HTMLResponse)
def fleet_health(request: Request):
    """ssd3 Ф7 T7.1 -- fleet-wide health triage (three-coordinate model, Ф1-Ф6).
    ONE route-context: get_fleet_health() + get_fleet_health_deltas() (each a single
    windowed query) plus 2 bounded exceptions -- <=10 get_score_series() calls
    (worsening sparklines) + one get_devices() call (model lookup). No per-device
    fan-out beyond those two. Distinct from the JSON API's ``GET /api/v1/health``."""
    from server.web.dashboard import _TEMPLATES  # local: see module docstring

    now = datetime.now(timezone.utc)
    rows = db.get_fleet_health()
    deltas = db.get_fleet_health_deltas()
    health_by_id = {r["device_id"]: r for r in rows}
    model_by_id = {d["device_id"]: d.get("model") for d in db.get_devices()}

    worsening = _worsening_selection(rows)
    sparklines = {
        r["device_id"]: _index_sparkline(db.get_score_series(r["device_id"], limit=30))
        for r in worsening
    }

    return _TEMPLATES.TemplateResponse(
        request,
        "health.html",
        {
            "kpi": _kpi_counts(rows, deltas, now),
            "state_dist": _state_distribution(rows),
            "heatmap": _heatmap(rows),
            "worsening": worsening,
            "sparklines": sparklines,
            "escalations": _escalations(deltas, health_by_id),
            "risk_models": _risk_models(rows, model_by_id),
        },
    )
