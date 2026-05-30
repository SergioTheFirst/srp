"""Ingest pipeline: store an envelope, then recompute scores for the device.

Keeping store+rescore together means an engineer sees fresh scores the moment
any message lands -- no batch job, no waiting. Scores are always recomputed
from the *latest* inventory + historical + heartbeat the server holds.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from shared.schema import Envelope, parse_payload

from server import db
from server.scoring import compute_day1_scores, compute_risk
from server.trust import (
    DOMAIN_SOURCES,
    CollectorStatus,
    SemanticStatus,
    SourceState,
    SourceTrust,
    compute_weight,
    derive_state,
    resolve_domain_trust,
    validate_source,
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Source-reading extraction helpers
# --------------------------------------------------------------------------- #


def _extract_reading(source: str, payload: dict) -> dict:
    """Extract the slice of payload that is semantically owned by *source*.

    Only material sources need a real reading; everything else returns {} and
    validate_source will mark it UNCHECKED, which can never become SUSPECT.
    """
    if source == "storage_reliability":
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
    source_health: dict,
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
        )
        db.store_inventory(did, ts, inv)
    elif env.msg_type == "historical":
        db.touch_device(did, ts, env.agent_version)
        db.store_historical(did, ts, env.payload)
    elif env.msg_type == "heartbeat":
        db.touch_device(did, ts, env.agent_version)
        db.store_heartbeat(did, ts, env.payload)
    elif env.msg_type == "events":
        db.touch_device(did, ts, env.agent_version)
        db.store_events(did, env.payload.get("events", []))

    if env.source_health:
        # Convert SourceHealth pydantic objects to plain dicts for evaluate_trust
        raw_health = {
            src: {"status": sh.status, "collected_at": sh.collected_at}
            for src, sh in env.source_health.items()
        }
        evaluate_trust(did, env.payload, raw_health, ts)

    scores = recompute_scores(did)
    return {
        "device_id": did,
        "msg_type": env.msg_type,
        "scores_updated": scores is not None,
        "scores": scores,
    }


# Bayesian failure class -> trust domain (3c). "memory" is intentionally ungated:
# RAM signals (WHEA/bugcheck) are not a trust domain in v1.
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
    """A device is untrusted when its identity source could not be trusted."""
    state = (trust.get("sources", {}).get("identity") or {}).get("state")
    return "untrusted" if state in ("unavailable", "suspect", "stale") else "ok"


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
    if trust:
        domains = trust.get("domains", {})
        _annotate_class_trust(risk["classes"], domains)
        risk_block["domains"] = domains
        risk_block["device_trust"] = _device_trust(trust)

    scores = {
        "performance": day1["performance"],
        "reliability": day1["reliability"],
        "wear": day1["wear"],
        "risk_exposure": day1["risk_exposure"],
        "risk": risk_block,
    }
    db.store_scores(device_id, _now_iso(), scores)
    return scores
