"""Ingest pipeline: store an envelope, then recompute scores for the device.

Keeping store+rescore together means an engineer sees fresh scores the moment
any message lands -- no batch job, no waiting. Scores are always recomputed
from the *latest* inventory + historical + heartbeat the server holds.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from shared.schema import CONTRACT_VERSION, Envelope, is_contract_compatible, parse_payload

from server import db
from server.analytics.battery import compute_battery_risk
from server.analytics.disk_fill import compute_disk_fill_risk
from server.analytics.storage import compute_storage_risk
from server.analytics.trends import compute_trends, trajectory_risk_score, trend_to_dict
from server.scoring import (
    compute_day1_score100,
    compute_day1_scores,
    compute_risk,
    legacy_value,
    score_to_dict,
)
from server.trust import (
    DOMAIN_SOURCES,
    GATE_PASS,
    CollectorStatus,
    SemanticStatus,
    SourceState,
    SourceTrust,
    compute_weight,
    derive_state,
    resolve_domain_trust,
    validate_source,
)

# W4.1: how much append-only history to feed the trend engine. Generous enough
# for a real slope, capped so one noisy device cannot make a query unbounded.
_TREND_HISTORY_LIMIT = 200


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _clock_drift_sec(received_at_iso: str, reported_ts_iso: Optional[str]) -> Optional[float]:
    """Signed seconds the server receipt leads the client-reported time (W0.2).

    Positive => client clock behind / message late; negative => client clock ahead.
    None when the client ts is unparseable.
    """
    recv = _parse_iso(received_at_iso)
    reported = _parse_iso(reported_ts_iso)
    if recv is None or reported is None:
        return None
    return (recv - reported).total_seconds()


# Collector statuses meaning "the source did not deliver" (newly-blocked detection, 3e).
_COLLECTOR_FAIL = (
    CollectorStatus.EMPTY,
    CollectorStatus.TIMEOUT,
    CollectorStatus.BLOCKED,
    CollectorStatus.ABSENT,
)

_CLOCK_DRIFT_FLAG_SEC = 300  # |received_at - ts| above this (s) is a clock-drift signal


# --------------------------------------------------------------------------- #
# Source-reading extraction helpers
# --------------------------------------------------------------------------- #


def _extract_reading(source: str, payload: dict) -> dict:
    """Extract the slice of payload that is semantically owned by *source*.

    Only material sources need a real reading; everything else returns {} and
    validate_source will mark it UNCHECKED, which can never become SUSPECT.
    """
    if source == "storage_reliability":
        # v1: validates the first disk only; multi-disk coverage deferred.
        items = payload.get("storage") or [{}]
        return items[0] if items else {}
    if source == "battery":
        return payload.get("battery") or {}
    if source == "free_space":
        return {"value": payload.get("free_space_pct")}
    if source == "throttle":
        return {"value": payload.get("cpu_perf_pct")}
    if source == "reliability":
        return {"value": payload.get("reliability_stability_index")}
    if source == "boot_time":
        return {"value": payload.get("avg_boot_ms")}
    # disk_latency, identity, events, and any unknown source:
    # not material → validate_source returns UNCHECKED
    return {}


def _build_source_trust_map(device_id: str) -> dict[str, SourceTrust]:
    """Reconstruct SourceTrust objects from all accumulated DB rows."""
    rows = db.get_source_trusts(device_id)
    result: dict[str, SourceTrust] = {}
    for src, row in rows.items():
        result[src] = SourceTrust(
            source=src,
            state=SourceState(row["state"]),
            weight=row["weight"],
            collector_status=CollectorStatus(row["collector_status"]),
            semantic_status=SemanticStatus(row["semantic_status"]),
            reason=row["reason"] or None,
        )
    return result


# --------------------------------------------------------------------------- #
# Trust evaluation
# --------------------------------------------------------------------------- #


def evaluate_trust(
    device_id: str,
    payload: dict,
    source_health: dict[str, dict[str, Any]],
    ts: str,
) -> dict[str, Any]:
    """Compute and persist per-source + per-domain trust for one envelope.

    Called from ingest_envelope when source_health is non-empty.
    """
    safe_payload = payload or {}

    for source, health in source_health.items():
        collector_status = CollectorStatus(health["status"])
        reading = _extract_reading(source, safe_payload)
        last_good = db.get_last_good(device_id, source)

        semantic_status, reason = validate_source(source, reading, last_good)

        applicable = not (source == "battery" and reading.get("present") is False)
        state = derive_state(
            collector_status,
            semantic_status,
            age_sec=None,
            stale_after_sec=None,
            applicable=applicable,
        )
        weight = compute_weight(state)

        db.upsert_source_trust(
            device_id,
            source,
            state.value,
            weight,
            collector_status.value,
            semantic_status.value,
            reason or "",
            ts,
        )

        if collector_status == CollectorStatus.OK and reading:
            db.set_last_good(device_id, source, reading, ts)

    # Aggregate accumulated per-source rows into domain trust
    source_map = _build_source_trust_map(device_id)

    domains: dict[str, Any] = {}
    for domain in DOMAIN_SOURCES:
        dt = resolve_domain_trust(domain, source_map)
        domains[domain] = {
            "state": dt.state.value,
            "weight": dt.weight,
            "contributing": dt.contributing,
            "dropped": dt.dropped,
            "reason": dt.reason,
        }

    sources_out: dict[str, Any] = {
        src: {
            "collector_status": st.collector_status.value,
            "semantic_status": st.semantic_status.value,
            "state": st.state.value,
            "weight": st.weight,
            "reason": st.reason,
            # 3e: a source that delivered before (has a last-good) but now fails is
            # "newly-blocked" (regressed) -- distinct from a source never seen.
            "regressed": st.collector_status in _COLLECTOR_FAIL
            and db.get_last_good(device_id, src) is not None,
        }
        for src, st in source_map.items()
    }

    result: dict[str, Any] = {"domains": domains, "sources": sources_out}
    db.store_trust(device_id, ts, result)
    return result


# --------------------------------------------------------------------------- #
# Main pipeline entry points
# --------------------------------------------------------------------------- #


def ingest_envelope(env: Envelope) -> dict[str, Any]:
    # Validate payload shape against the typed model (raises ValueError on bad type).
    parse_payload(env.msg_type, env.payload)

    did, ts = env.device_id, env.ts
    # W0.2: stamp server receipt + clock drift; never trust the client clock for
    # staleness / trends / windows. ts is retained as the client-reported time.
    received_at = _now_iso()
    drift = _clock_drift_sec(received_at, ts)
    if env.msg_type == "inventory":
        inv = env.payload
        db.upsert_device(
            did,
            ts,
            env.agent_version,
            hostname=inv.get("hostname"),
            manufacturer=inv.get("manufacturer"),
            model=inv.get("model"),
            chassis=inv.get("chassis"),
            site_code=env.site_code,
            site_name=env.site_name,
            received_at=received_at,
            last_reported_ts=ts,
            clock_drift_sec=drift,
        )
        db.store_inventory(did, ts, inv)
    elif env.msg_type == "historical":
        db.touch_device(
            did,
            ts,
            env.agent_version,
            site_code=env.site_code,
            site_name=env.site_name,
            received_at=received_at,
            last_reported_ts=ts,
            clock_drift_sec=drift,
        )
        db.store_historical(did, ts, env.payload, received_at=received_at, clock_drift_sec=drift)
    elif env.msg_type == "heartbeat":
        db.touch_device(
            did,
            ts,
            env.agent_version,
            site_code=env.site_code,
            site_name=env.site_name,
            received_at=received_at,
            last_reported_ts=ts,
            clock_drift_sec=drift,
        )
        db.store_heartbeat(did, ts, env.payload, received_at=received_at, clock_drift_sec=drift)
    elif env.msg_type == "events":
        db.touch_device(
            did,
            ts,
            env.agent_version,
            site_code=env.site_code,
            site_name=env.site_name,
            received_at=received_at,
            last_reported_ts=ts,
            clock_drift_sec=drift,
        )
        db.store_events(
            did, env.payload.get("events", []), received_at=received_at, clock_drift_sec=drift
        )

    if env.source_health:
        # Convert SourceHealth pydantic objects to plain dicts for evaluate_trust
        raw_health = {
            src: {"status": sh.status, "collected_at": sh.collected_at}
            for src, sh in env.source_health.items()
        }
        evaluate_trust(did, env.payload, raw_health, ts)

    # W4.0: events never feed scoring -- recompute_scores reads only the latest
    # inventory / historical / heartbeat, never the events table. Rescoring on an
    # events message is pure waste (and now drags in the O(n^2) W4.1 trend pass),
    # so we store the events above and skip the recompute. Message types that do
    # change scores still rescore synchronously, so fresh scores land on ingest.
    scores = None if env.msg_type == "events" else recompute_scores(did)
    return {
        "device_id": did,
        "msg_type": env.msg_type,
        "scores_updated": scores is not None,
        "scores": scores,
        # W0.4 capability negotiation: tell the agent our contract version and
        # whether we consider it compatible. A mismatch is flagged, never a reason
        # to drop telemetry (the ingest above already stored it).
        "server_contract_version": CONTRACT_VERSION,
        "contract_compatible": is_contract_compatible(env.agent_version),
    }


# Bayesian failure class -> trust domain (3c). "memory" is intentionally ungated:
# RAM signals (WHEA/bugcheck) are not a trust domain in v1. The disk_fill/boot
# domains are tracked for lineage but gate no scoring class in v1 (no class maps).
_CLASS_DOMAIN = {
    "storage": "storage",
    "battery": "battery",
    "power_thermal": "thermal",
    "stability": "os_stability",
}


def _annotate_class_trust(classes: list, domains: dict) -> None:
    """Tag each bayesian class with its mapped domain's trust state (None if ungated)."""
    for c in classes:
        dom = _CLASS_DOMAIN.get(c.get("name"))
        c["trust"] = domains.get(dom, {}).get("state") if dom else None


def _device_trust(trust: dict) -> str:
    """A device is untrusted when its identity source fails the trust gate.

    Resolved through SourceState/GATE_PASS (single source of truth) so a future
    gate-fail state is covered automatically; an unknown DB value does not flag.
    """
    raw = (trust.get("sources", {}).get("identity") or {}).get("state")
    if raw is None:
        return "ok"
    try:
        state = SourceState(raw)
    except ValueError:
        return "ok"
    return "untrusted" if state not in GATE_PASS else "ok"


def recompute_scores(device_id: str) -> Optional[dict[str, Any]]:
    inv = db.get_inventory(device_id)
    hist = db.get_historical(device_id)
    hbs = db.get_recent_heartbeats(device_id, limit=1)
    hb = hbs[0] if hbs else None
    if inv is None and hist is None and hb is None:
        return None

    day1 = compute_day1_scores(inv, hist, hb)
    risk = compute_risk(inv, hist, hb)
    risk_block: dict[str, Any] = {
        "classes": risk["classes"],
        "top": risk["top"],
        "overall": risk["overall"],
        "day1_factors": day1["factors"],
    }

    # 3c: gate the explainable risk by the per-domain trust computed on ingest.
    trust = db.get_trust(device_id)
    device_trust = "ok"
    if trust:
        domains = trust.get("domains", {})
        _annotate_class_trust(risk["classes"], domains)
        risk_block["domains"] = domains
        device_trust = _device_trust(trust)
        risk_block["device_trust"] = device_trust
        if device_trust == "untrusted":
            for c in risk["classes"]:
                c["trust"] = "unknown"
        regressed = [s for s, v in trust.get("sources", {}).items() if v.get("regressed")]
        if regressed:
            risk_block["regressed_sources"] = sorted(regressed)

    # W0.5: wrap the day-1 numbers in the confidence-gated Score100 envelope.
    # Missing/untrusted telemetry must not read as healthy (contract: UNKNOWN over
    # false confidence). Legacy numeric columns are derived from the envelope via
    # legacy_value() so the current API/dashboard keep working; the full Score100
    # map rides inside the risk blob (no DB schema churn).
    clock_drift = bool(hb and abs(hb.get("clock_drift_sec") or 0.0) > _CLOCK_DRIFT_FLAG_SEC)
    score100 = compute_day1_score100(
        day1, inv, hist, hb, trust=trust, device_trust=device_trust, clock_drift=clock_drift
    )
    risk_block["score100"] = {name: score_to_dict(s) for name, s in score100.items()}

    # W4.1: deterministic trajectory (slopes + ETA) over the append-only series.
    # Computed here (single source of truth) so the stored blob, dashboard and the
    # /diagnostics endpoint all read one consistent result. Same gating as W0.5:
    # untrusted identity withholds; insufficient history -> UNKNOWN (no fake ETA).
    hist_series = db.get_historical_series(device_id, limit=_TREND_HISTORY_LIMIT)
    hb_series = db.get_recent_heartbeats(device_id, limit=_TREND_HISTORY_LIMIT)
    trends = compute_trends(hist_series, hb_series)
    trajectory = trajectory_risk_score(trends, device_trust=device_trust)
    risk_block["score100"]["trajectory_risk"] = score_to_dict(trajectory)
    risk_block["trajectory"] = {name: trend_to_dict(t) for name, t in trends.items()}

    # W4.2: deterministic storage-health engine (SMART-led; latency only confirms).
    # Current-state verdict over the latest reading -- the wear *trend*/ETA lives in
    # the trajectory engine above. Same gating (untrusted -> withheld, no SMART ->
    # UNKNOWN); surfaced alongside trajectory in the score blob and /diagnostics.
    storage_risk = compute_storage_risk(hist, hb, device_trust=device_trust)
    risk_block["score100"]["storage_risk"] = score_to_dict(storage_risk)

    # W4.2: deterministic battery-health engine (capacity fade leads; cycles only
    # grade). Current-state verdict over the latest reading -- the wear *trend*/ETA
    # lives in the trajectory engine above. Same gating (untrusted -> withheld; no
    # battery -> not applicable; present-but-no-metric -> UNKNOWN); confidence caps
    # at medium because WMI cannot see swelling (a clean capacity reading is not a
    # safety clearance). Surfaced alongside storage in the blob and /diagnostics.
    battery_risk = compute_battery_risk(hist, device_trust=device_trust)
    risk_block["score100"]["battery_risk"] = score_to_dict(battery_risk)

    # W4.2: deterministic disk-fill / servicing-collapse engine (current-state).
    # Free-space risk grades on the *median* recent level so a Windows-Update cleanup
    # rebound (one transient dip) does not alarm while a persistently-full drive does;
    # WindowsUpdateClient failures confirm/amplify (or, on a healthy disk, flag a real
    # "not patching" risk). Same gating (untrusted -> withheld; no data -> UNKNOWN).
    # The depletion *slope/ETA* lives in the trajectory engine above, not here.
    events = db.get_recent_events(device_id, limit=_TREND_HISTORY_LIMIT)
    disk_fill_risk = compute_disk_fill_risk(hb_series, events, device_trust=device_trust)
    risk_block["score100"]["disk_fill_risk"] = score_to_dict(disk_fill_risk)

    scores = {
        "performance": legacy_value(score100["performance"]),
        "reliability": legacy_value(score100["reliability"]),
        "wear": legacy_value(score100["wear"]),
        "risk_exposure": legacy_value(score100["risk_exposure"]),
        "risk": risk_block,
    }
    db.store_scores(device_id, _now_iso(), scores)
    return scores
