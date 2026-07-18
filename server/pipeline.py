"""Ingest pipeline: store an envelope, then recompute scores for the device.

Keeping store+rescore together means an engineer sees fresh scores the moment
any message lands -- no batch job, no waiting. Scores are always recomputed
from the *latest* inventory + historical + heartbeat the server holds.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Optional

from shared.schema import CONTRACT_VERSION, Envelope, is_contract_compatible, parse_payload

from server import db
from server.analytics.disk_fill import compute_disk_fill_risk
from server.analytics.errchain import analyze_events
from server.analytics.fleet_anomaly import compute_fleet_anomaly_risk
from server.analytics.health import compute_health
from server.analytics.network_risk import compute_network_risk
from server.analytics.os_degradation import compute_os_degradation_risk
from server.analytics.software_aging import compute_software_aging_risk
from server.analytics.storage import compute_storage_risk, worst_disk_key
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
    MATERIAL_SOURCES,
    CollectorStatus,
    SemanticStatus,
    SourceState,
    SourceTrust,
    compute_weight,
    derive_state,
    resolve_domain_trust,
    validate_source,
)

# Sources the server actually knows how to gate/validate: every domain's required +
# optional sources (server/trust/domains.py) plus the non-domain sources the pipeline
# recognizes but that gate no domain (identity/events/certificates/print_jobs are real
# client sources -- see client/collectors/sources.py; event_counts is validated but not
# yet emitted by any collector, W0.1 wiring). evaluate_trust skips anything outside this
# set -- a retired/legacy source a stale agent still sends, a forged name, whatever --
# silently, no trust row. Keeps old-agent envelopes ingesting clean forever.
_KNOWN_TRUST_SOURCES = (
    frozenset(
        src for spec in DOMAIN_SOURCES.values() for src in (*spec["required"], *spec["optional"])
    )
    | MATERIAL_SOURCES
    | frozenset({"identity", "events", "certificates", "print_jobs"})
)

# W4.1: how much append-only history to feed the trend engine. Generous enough
# for a real slope, capped so one noisy device cannot make a query unbounded.
_TREND_HISTORY_LIMIT = 200

# ssd3 Ф3 (T3.3): errchain needs a wider window than the trend engine (30d of
# raw events, not just a slope's worth of points) but still capped per device.
_ERRCHAIN_EVENT_LIMIT = 1000

# ssd3 Ф5 (T5.4): recurrent_weeks widens from the 30d raw window to 90d once
# daily rollups exist -- more lookback than raw event retention alone keeps.
_ROLLUP_LOOKBACK_DAYS = 90

if TYPE_CHECKING:
    from server.rescore_queue import RescoreQueue

_RESCORE_QUEUE: Optional["RescoreQueue"] = None


def set_rescore_queue(queue: Optional["RescoreQueue"]) -> None:
    """W4.0: включить/выключить фоновый rescore (None = синхронно, как раньше)."""
    global _RESCORE_QUEUE
    _RESCORE_QUEUE = queue


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


# ssd3 Ф6 (T6.2): delta_7d = current health index minus the index of the first
# stored row older than 7 days. Reuses _TREND_HISTORY_LIMIT (200) for the fetch.
# Cadence check (ingest_envelope, ~L459): inventory/historical/heartbeat each
# trigger their own recompute_scores call, and client/config.py sends all three
# on the same 14400s (4h) cycle -- so ~3 new score rows land per 4h, not 1
# (events/print_jobs/liveness/update_status never rescore). That is ~18
# rows/day, so 200 rows reaches back ~11 days: comfortably past the 7-day mark
# (scores retention is 5000, so row depth is never the limiting cap here).
_HEALTH_DELTA_DAYS = 7


def _health_delta_7d(device_id: str, index: Optional[float]) -> Optional[float]:
    """Health-index change vs the first stored score row older than 7 days.

    ``None`` until such a row exists ("первые 7 дней норма") or when either index
    is unknown. The series is newest-first; the first row past the 7-day boundary
    is the comparison point (T6.2).
    """
    if index is None:
        return None
    cutoff = datetime.now(timezone.utc) - timedelta(days=_HEALTH_DELTA_DAYS)
    for row in db.get_score_series(device_id, limit=_TREND_HISTORY_LIMIT):
        ts = _parse_iso(row.get("ts"))
        if ts is None or ts >= cutoff:
            continue
        older = ((row.get("risk") or {}).get("health") or {}).get("index")
        return None if older is None else round(index - older, 1)
    return None


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
    if source == "smart":
        # Deep-SMART decision-material slice (ssd3 Ф1): same first-disk row as
        # storage_reliability, narrowed to the fields validate_smart_item checks.
        items = payload.get("storage") or [{}]
        item = items[0] if items else {}
        return {
            "serial_hash": item.get("serial_hash"),
            "temperature_c": item.get("temperature_c"),
            "power_on_hours": item.get("power_on_hours"),
            "nvme_spare_pct": item.get("nvme_spare_pct"),
            "nvme_spare_threshold_pct": item.get("nvme_spare_threshold_pct"),
            "nvme_percentage_used": item.get("nvme_percentage_used"),
            "nvme_media_errors": item.get("nvme_media_errors"),
            "nvme_unsafe_shutdowns": item.get("nvme_unsafe_shutdowns"),
            "smart_attrs": item.get("smart_attrs") or {},
        }
    if source == "free_space":
        return {"value": payload.get("free_space_pct")}
    if source == "throttle":
        return {"value": payload.get("cpu_perf_pct")}
    if source == "reliability":
        return {"value": payload.get("reliability_stability_index")}
    if source == "boot_time":
        return {"value": payload.get("avg_boot_ms")}
    if source == "network":
        # Decision-material slice only (quality probes + Wi-Fi signal); neighbors/
        # connections are bulk map data and stay out of last_good.
        adapters = payload.get("network_adapters") or []
        return {
            "quality": payload.get("network_quality") or [],
            "adapters_count": len(adapters),
            "signal_pcts": [
                a.get("signal_pct")
                for a in adapters
                if isinstance(a, dict) and a.get("signal_pct") is not None
            ],
        }
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
        if source not in _KNOWN_TRUST_SOURCES:
            # Unknown to this server -- a retired source a stale agent still sends,
            # a forged name, whatever. Silently ignored: no trust row, no exception
            # (contract: an old/odd envelope must still ingest clean).
            continue
        collector_status = CollectorStatus(health["status"])
        reading = _extract_reading(source, safe_payload)
        last_good = db.get_last_good(device_id, source)

        semantic_status, reason = validate_source(source, reading, last_good)

        state = derive_state(
            collector_status,
            semantic_status,
            age_sec=None,
            stale_after_sec=None,
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

# Message types that carry no real telemetry (liveness = {alive} only;
# update_status = self-update state) and must never enter trust evaluation --
# a forged envelope could otherwise smuggle a fabricated source_health reading
# through the msg_type-agnostic gate below (the same HIGH finding closed for
# liveness in B2 ops-fixes now also covers update_status).
_NO_TRUST_MSG_TYPES = frozenset({"liveness", "update_status"})


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
            org_code=env.org_code,
            dept_code=env.dept_code,
            comment=env.comment,
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
            hostname=env.hostname,
            site_code=env.site_code,
            site_name=env.site_name,
            org_code=env.org_code,
            dept_code=env.dept_code,
            comment=env.comment,
            received_at=received_at,
            last_reported_ts=ts,
            clock_drift_sec=drift,
        )
        db.store_historical(did, ts, env.payload, received_at=received_at, clock_drift_sec=drift)
        # ssd3 Ф2: one series per PHYSICAL DISK (keyed by serial_hash), so
        # recurrence/acceleration evidence survives an OS reinstall the same
        # way historical's device-envelope series does not.
        db.store_disk_readings(did, env.payload.get("storage", []), ts, received_at)
        # printview: persist spooler {queue-name -> printer-IP} hints so print
        # views can resolve a print job's printer to its IP (server-side, no agent
        # or contract change). env.payload is the RAW dict (the validated parse is
        # not retained here), so store_printer_ip_hints re-applies the RFC1918
        # filter AND the count/length caps -- it is the operative guard.
        db.store_printer_ip_hints(did, env.payload.get("printer_ports", []))
    elif env.msg_type == "heartbeat":
        db.touch_device(
            did,
            ts,
            env.agent_version,
            hostname=env.hostname,
            site_code=env.site_code,
            site_name=env.site_name,
            org_code=env.org_code,
            dept_code=env.dept_code,
            comment=env.comment,
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
            hostname=env.hostname,
            site_code=env.site_code,
            site_name=env.site_name,
            org_code=env.org_code,
            dept_code=env.dept_code,
            comment=env.comment,
            received_at=received_at,
            last_reported_ts=ts,
            clock_drift_sec=drift,
        )
        db.store_events(
            did, env.payload.get("events", []), received_at=received_at, clock_drift_sec=drift
        )
    elif env.msg_type == "print_jobs":
        db.touch_device(
            did,
            ts,
            env.agent_version,
            hostname=env.hostname,
            site_code=env.site_code,
            site_name=env.site_name,
            org_code=env.org_code,
            dept_code=env.dept_code,
            comment=env.comment,
            received_at=received_at,
            last_reported_ts=ts,
            clock_drift_sec=drift,
        )
        db.store_print_jobs(did, env.payload.get("jobs", []), received_at=received_at)
    elif env.msg_type == "liveness":
        # Только last_seen: ни строк телеметрии, ни trust-оценки (source_health
        # у liveness пуст), ни рескоринга (skip-set ниже). Дешёвый пинг «я жив».
        db.touch_device(
            did,
            ts,
            env.agent_version,
            hostname=env.hostname,
            site_code=env.site_code,
            site_name=env.site_name,
            org_code=env.org_code,
            dept_code=env.dept_code,
            comment=env.comment,
            received_at=received_at,
            last_reported_ts=ts,
            clock_drift_sec=drift,
        )
    elif env.msg_type == "update_status":
        # Статус самообновления агента: last_seen + update_state/error/checked_at.
        # available_version контрактом принимается, но сервер его не хранит --
        # актуальную версию сервер знает из своего манифеста (server/updates.py).
        db.touch_device(
            did,
            ts,
            env.agent_version,
            hostname=env.hostname,
            site_code=env.site_code,
            site_name=env.site_name,
            org_code=env.org_code,
            dept_code=env.dept_code,
            comment=env.comment,
            received_at=received_at,
            last_reported_ts=ts,
            clock_drift_sec=drift,
        )
        db.set_update_status(
            did,
            env.payload.get("state"),
            env.payload.get("error"),
            env.payload.get("checked_at"),
        )

    # liveness/update_status carry no telemetry by contract (LivenessPayload =
    # {alive}; UpdateStatusPayload = self-update state) and must never enter trust
    # evaluation -- enforced here, not merely assumed from the client never
    # populating source_health, since a forged envelope could otherwise smuggle a
    # fabricated "ok" source reading through this msg_type-agnostic gate.
    if env.msg_type not in _NO_TRUST_MSG_TYPES and env.source_health:
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
    no_rescore = {"events", "print_jobs", "liveness", "update_status"}
    if env.msg_type in no_rescore:
        scores = None
    elif _RESCORE_QUEUE is not None:
        # W4.0: писать быстро, пересчитывать асинхронно -- recompute уходит из
        # HTTP-запроса; свежие скоры появятся в /api/v1/devices после воркера.
        _RESCORE_QUEUE.submit(did)
        scores = None
    else:
        scores = recompute_scores(did)
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

    # 3c: gate the explainable risk by the per-domain trust computed on ingest.
    trust = db.get_trust(device_id)
    device_trust = "ok"
    if trust:
        device_trust = _device_trust(trust)

    # P0-5 (stoperrors.md): compute_risk needs the REAL gate, not a postfactum
    # label -- a gate-failed domain must never produce a number in the first
    # place. Keyed by bayesian CLASS name via _CLASS_DOMAIN (bayesian.py stays
    # domain-vocabulary-agnostic). Identity-untrusted is a superset gate: every
    # mapped class withholds regardless of its own domain's state, mirroring
    # the existing cosmetic override a few lines below.
    class_trust: Optional[dict[str, str]] = None
    if trust:
        domains_raw = trust.get("domains", {})
        class_trust = {
            cls: (
                "unknown"
                if device_trust == "untrusted"
                else domains_raw.get(dom, {}).get("state", "unknown")
            )
            for cls, dom in _CLASS_DOMAIN.items()
        }

    # W0.5: wrap the day-1 numbers in the confidence-gated Score100 envelope.
    # Missing/untrusted telemetry must not read as healthy (contract: UNKNOWN over
    # false confidence). Legacy numeric columns are derived from the envelope via
    # legacy_value() so the current API/dashboard keep working; the full Score100
    # map rides inside the risk blob (no DB schema churn).
    clock_drift = bool(hb and abs(hb.get("clock_drift_sec") or 0.0) > _CLOCK_DRIFT_FLAG_SEC)
    score100 = compute_day1_score100(
        day1, inv, hist, hb, trust=trust, device_trust=device_trust, clock_drift=clock_drift
    )

    # W4.1: deterministic trajectory (slopes + ETA) over the append-only series.
    # Computed here (single source of truth) so the stored blob, dashboard and the
    # /diagnostics endpoint all read one consistent result. Same gating as W0.5:
    # untrusted identity withholds; insufficient history -> UNKNOWN (no fake ETA).
    hist_series = db.get_historical_series(device_id, limit=_TREND_HISTORY_LIMIT)
    hb_series = db.get_recent_heartbeats(device_id, limit=_TREND_HISTORY_LIMIT)
    # ssd3 Ф2: pick the worst disk from D-points of its LATEST reading alone
    # (no series yet -- breaks the "trends need the engine, the engine needs
    # the worst disk" cycle), THEN fetch its own series, THEN compute trends
    # over it, THEN hand both to the engine. Order is fixed (§1.6/T2.2).
    worst_key = worst_disk_key(hist)
    disk_series = (
        db.get_disk_series(device_id, worst_key, limit=_TREND_HISTORY_LIMIT) if worst_key else None
    )
    trends = compute_trends(hist_series, hb_series, disk_series=disk_series)
    trajectory = trajectory_risk_score(trends, device_trust=device_trust)

    # W4.2: domain engines — computed before the Bayesian prioritizer so their
    # outputs can serve as primary inputs (D5 thin prioritizer design, W4.3).
    # ssd3 Ф3: errchain must run before compute_storage_risk -- the engine's
    # chain-stage/burstiness/early-event rules (wired in Ф2) read it via chain=.
    chain_events = db.get_recent_events(device_id, limit=_ERRCHAIN_EVENT_LIMIT)
    rollup_counts = db.get_event_rollups(device_id, _ROLLUP_LOOKBACK_DAYS)
    chain = analyze_events(
        chain_events, now=datetime.now(timezone.utc), rollup_counts=rollup_counts
    )
    rule_stats = db.get_rule_stats()
    storage_risk = compute_storage_risk(
        hist,
        hb,
        device_trust=device_trust,
        disk_series=disk_series,
        chain=chain,
        trends=trends,
        rule_stats=rule_stats,
    )
    events = db.get_recent_events(device_id, limit=_TREND_HISTORY_LIMIT)
    disk_fill_risk = compute_disk_fill_risk(hb_series, events, device_trust=device_trust)
    os_degradation_risk = compute_os_degradation_risk(hist, device_trust=device_trust)
    _model, _site = db.get_device_model_site(device_id)
    cohort_stats = db.get_fleet_cohort_stats(_model, _site)
    fleet_anomaly_risk = compute_fleet_anomaly_risk(cohort_stats, device_trust=device_trust)
    # Phase 2: per-device axis only (own data); the subnet anomaly is read-side (D7).
    net_domain = ((trust or {}).get("domains") or {}).get("network") or {}
    network_risk = compute_network_risk(
        hist, device_trust=device_trust, domain_state=net_domain.get("state")
    )
    # ssd3 Ф4: session-scoped handle/memory-leak verdict (pure Resilience, K2 --
    # never contributes Damage). Needs only the heartbeat series already fetched
    # above for the trend engine.
    software_aging_risk = compute_software_aging_risk(hb_series, device_trust=device_trust)

    # W4.3: thin Bayesian prioritizer over domain engines (D5). domain_values feeds
    # the W4.2 outputs (0..100) as supplementary log-odds factors into each class so
    # the prioritizer reads from the domain layer rather than re-deriving telemetry.
    # KP41 demoted to conditional enhancer; WHEA removed from power_thermal + memory
    # (D6). overall now on 0..100 scale consistent with risk_exposure and W4.2 axes.
    # Only keys consumed by _domain_lo() calls inside bayesian.py class functions.
    # trajectory_risk and fleet_anomaly_risk are not yet wired into any Bayesian class.
    domain_values: dict[str, Optional[float]] = {
        "storage_risk": storage_risk.value,
        "os_degradation_risk": os_degradation_risk.value,
        "disk_fill_risk": disk_fill_risk.value,
        "software_aging_risk": software_aging_risk.value,
    }
    risk = compute_risk(
        inv,
        hist,
        hb,
        domain_values=domain_values,
        app_hang_count_30d=chain.counts.get("app_hang", 0),
        domain_trust=class_trust,
    )

    risk_block: dict[str, Any] = {
        "classes": risk["classes"],
        "top": risk["top"],
        "overall": risk["overall"],
        "day1_factors": day1["factors"],
        "errchain": asdict(chain),
    }

    if trust:
        domains = trust.get("domains", {})
        _annotate_class_trust(risk["classes"], domains)
        risk_block["domains"] = domains
        risk_block["device_trust"] = device_trust
        if device_trust == "untrusted":
            for c in risk["classes"]:
                c["trust"] = "unknown"
        regressed = [s for s, v in trust.get("sources", {}).items() if v.get("regressed")]
        if regressed:
            risk_block["regressed_sources"] = sorted(regressed)

    risk_block["score100"] = {name: score_to_dict(s) for name, s in score100.items()}
    risk_block["score100"]["trajectory_risk"] = score_to_dict(trajectory)
    risk_block["score100"]["storage_risk"] = score_to_dict(storage_risk)
    # ssd3 Ф2: promote the coordinate tags (D/R) out of source_lineage to a
    # top-level sibling key -- additive, old readers of storage_risk never
    # look for "coords" and are unaffected.
    risk_block["score100"]["storage_risk"]["coords"] = storage_risk.source_lineage.get(
        "coords", {"damage": 0.0, "resilience_loss": 0.0, "flags": []}
    )
    risk_block["score100"]["disk_fill_risk"] = score_to_dict(disk_fill_risk)
    risk_block["score100"]["os_degradation_risk"] = score_to_dict(os_degradation_risk)
    risk_block["score100"]["fleet_anomaly_risk"] = score_to_dict(fleet_anomaly_risk)
    risk_block["score100"]["network_risk"] = score_to_dict(network_risk)
    risk_block["score100"]["software_aging_risk"] = score_to_dict(software_aging_risk)
    # ssd3 Ф4: same coords-promotion convention as storage_risk above -- Ф6 reads
    # both engines' flags from the same top-level shape.
    risk_block["score100"]["software_aging_risk"]["coords"] = (
        software_aging_risk.source_lineage.get("coords", {"flags": []})
    )
    risk_block["trajectory"] = {name: trend_to_dict(t) for name, t in trends.items()}

    # ssd3 Ф6 (T6.2): assemble the coordinate verdict from the axes above and
    # persist it inside the risk blob (no schema/table change). prev_health = the
    # health block of the last stored row (ratchet input; None on first recompute).
    # cohort_stats (fetched above for the fleet-anomaly axis) now also carries
    # boot_p90_ms, the field health.py's cohort context-factor reads.
    prev_rows = db.get_score_series(device_id, limit=1)
    prev_health = prev_rows[0]["risk"].get("health") if prev_rows else None
    verdict = compute_health(
        score100_axes=risk_block["score100"],
        bayes=risk,
        trends=risk_block["trajectory"],
        errchain=risk_block["errchain"],
        cohort=cohort_stats,
        prev_health=prev_health,
    )
    # worst_disk: same source health.py's own compute_health reads for cur_disk
    # (score100_axes["storage_risk"]["source_lineage"]["worst_disk"]) -- not the
    # simpler worst_disk_key(hist) pre-pass above, which can disagree with the
    # storage engine's own (more authoritative) pick. Persisting it here is what
    # lets the ratchet's disk-replacement branch compare prev vs. current on the
    # next recompute (health.py module docstring; previously always None/dormant).
    worst_disk = risk_block["score100"]["storage_risk"].get("source_lineage", {}).get("worst_disk")
    risk_block["health"] = {
        **asdict(verdict),
        "delta_7d": _health_delta_7d(device_id, verdict.index),
        "worst_disk": worst_disk,
    }

    scores = {
        "performance": legacy_value(score100["performance"]),
        "reliability": legacy_value(score100["reliability"]),
        "wear": legacy_value(score100["wear"]),
        "risk_exposure": legacy_value(score100["risk_exposure"]),
        "risk": risk_block,
    }
    db.store_scores(device_id, _now_iso(), scores)
    return scores
