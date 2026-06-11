"""Phase-2 network health engine: current-state verdict for one device.

Leading signal = measured quality to the *gateway* -- the one target that must
answer (graded packet loss; latency confirms). APIPA (169.254.x on an up adapter)
is a strong standalone failure: DHCP never answered, the NIC has no real network.
Weak Wi-Fi signal contributes mildly. ICMP honesty (D5): when every probe of the
machine lost 100% with no reply, firewall-vs-outage is undecidable from this
vantage -> blind spot in missing_evidence, never an alarm; full loss only to DNS
targets while the gateway answers is likewise ignored (DNS boxes drop ICMP).

Confidence caps at medium (D11): one vantage point and coarse ICMP cannot prove
the path beyond the gateway. Gating mirrors the other W4.2 engines: untrusted
identity withholds; absent telemetry -> UNKNOWN (checked first, so an old agent
reads "no data" not "gate failed"); a gate-failed network trust domain withholds.
The latency *trend* lives in trends.py (gateway_latency); this is current state.
"""

from __future__ import annotations

from typing import Any, Optional

from server.scoring.score100 import (
    Direction,
    Factor,
    Score100,
    ScoreConfidence,
    band_for_risk_score,
    make_score100,
)

_BLIND_SPOT = "видимость только с этой машины: путь за пределами шлюза не наблюдается"

_GW_LOSS_FULL = 45.0
_GW_LOSS_HEAVY = 30.0  # >= 20% loss
_GW_LOSS_LIGHT = 15.0  # >= 5% loss
_GW_LAT_HIGH = 15.0  # >= 100 ms
_GW_LAT_WARN = 8.0  # >= 30 ms
_DNS_PARTIAL = 8.0
_APIPA = 35.0
_WIFI_WEAK = 12.0  # < 30%
_WIFI_LOW = 6.0  # < 50%


def _f(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _icmp_filtered(rows: list[dict[str, Any]]) -> bool:
    if not rows:
        return False
    return all(
        (_f(q.get("loss_pct")) or 0.0) >= 100.0 and q.get("latency_ms") is None for q in rows
    )


def _withheld(missing: list[str], lineage: dict[str, Any], reason: str) -> Score100:
    return make_score100(
        None,
        "higher_is_worse",
        "unknown",
        "unknown",
        missing_evidence=missing,
        source_lineage=lineage,
        reason=reason,
    )


def compute_network_risk(
    historical: Optional[dict[str, Any]],
    *,
    device_trust: str = "ok",
    domain_state: Optional[str] = None,
) -> Score100:
    """Deterministic network risk for one device (0..100, higher = worse)."""
    direction: Direction = "higher_is_worse"

    if device_trust == "untrusted":
        return _withheld(
            ["идентификация не подтверждена"],
            {"identity": "untrusted"},
            "идентификатор устройства не подтверждён (контракт §7)",
        )

    hist = historical or {}
    adapters = [a for a in (hist.get("network_adapters") or []) if isinstance(a, dict)]
    quality = [q for q in (hist.get("network_quality") or []) if isinstance(q, dict)]
    if not adapters and not quality:
        return _withheld(
            ["нет сетевой телеметрии"],
            {},
            "агент не передал сетевые данные (UNKNOWN — ложная уверенность недопустима)",
        )

    if domain_state == "unknown":
        return _withheld(
            ["источник network не прошёл проверку доверия"],
            {"network_domain": "unknown"},
            "сетевой источник не прошёл гейт доверия — оценка скрыта",
        )

    value = 0.0
    factors: list[Factor] = []
    missing: list[str] = [_BLIND_SPOT]

    def hit(label: str, delta: float) -> None:
        nonlocal value
        value += delta
        factors.append({"label": label, "delta": round(delta, 1)})

    icmp_blocked = _icmp_filtered(quality)
    usable = [] if icmp_blocked else quality
    if icmp_blocked:
        missing.append(
            "все пробы без ответа: либо ICMP блокируется фаерволом, либо связи нет — "
            "отличить с одной машины нельзя"
        )

    gw_rows = [q for q in usable if q.get("target_kind") == "gateway"]
    dns_rows = [q for q in usable if q.get("target_kind") == "dns"]

    gw_measured = False
    worst_gw = max(gw_rows, key=lambda q: _f(q.get("loss_pct")) or 0.0, default=None)
    if worst_gw is not None and _f(worst_gw.get("loss_pct")) is not None:
        gw_measured = True
        loss = _f(worst_gw.get("loss_pct")) or 0.0
        lat = _f(worst_gw.get("latency_ms"))
        target = worst_gw.get("target")
        if loss >= 100.0:
            hit(f"шлюз {target} не отвечает на ping (потери 100%)", _GW_LOSS_FULL)
        else:
            if loss >= 20.0:
                hit(f"потери до шлюза {target}: {loss:.0f}%", _GW_LOSS_HEAVY)
            elif loss >= 5.0:
                hit(f"потери до шлюза {target}: {loss:.0f}%", _GW_LOSS_LIGHT)
            if lat is not None and lat >= 100.0:
                hit(f"высокая задержка до шлюза: {lat:.0f} мс", _GW_LAT_HIGH)
            elif lat is not None and lat >= 30.0:
                hit(f"повышенная задержка до шлюза: {lat:.0f} мс", _GW_LAT_WARN)
    elif not icmp_blocked:
        missing.append("нет измерений качества связи до шлюза")

    dns_partial = [q for q in dns_rows if 5.0 <= (_f(q.get("loss_pct")) or 0.0) < 100.0]
    if dns_partial:
        worst_dns = max(dns_partial, key=lambda q: _f(q.get("loss_pct")) or 0.0)
        hit(
            f"потери до DNS {worst_dns.get('target')}: "
            f"{(_f(worst_dns.get('loss_pct')) or 0.0):.0f}%",
            _DNS_PARTIAL,
        )
    dns_full_ignored = sum(1 for q in dns_rows if (_f(q.get("loss_pct")) or 0.0) >= 100.0)

    for a in adapters:
        if a.get("up") and any(str(ip).startswith("169.254.") for ip in (a.get("ipv4") or [])):
            hit(
                f"адаптер «{a.get('name') or '?'}» без DHCP-адреса (APIPA 169.254.x) — "
                "сети фактически нет",
                _APIPA,
            )

    for a in adapters:
        sig = _f(a.get("signal_pct"))
        if a.get("up") and a.get("kind") == "wifi" and sig is not None:
            if sig < 30.0:
                hit(f"слабый сигнал Wi-Fi: {sig:.0f}%", _WIFI_WEAK)
            elif sig < 50.0:
                hit(f"невысокий сигнал Wi-Fi: {sig:.0f}%", _WIFI_LOW)

    value = max(0.0, min(100.0, value))
    confidence: ScoreConfidence = "medium" if gw_measured else "low"

    reason = ""
    if value == 0.0:
        reason = (
            "связь со шлюзом в норме"
            if gw_measured
            else "тревожных сигналов нет, но качество связи не измерено"
        )

    lineage = {
        "adapters_total": len(adapters),
        "adapters_up": sum(1 for a in adapters if a.get("up")),
        "quality_targets": len(quality),
        "gateway_measured": gw_measured,
        "icmp_blocked": icmp_blocked,
        "dns_full_loss_ignored": dns_full_ignored,
    }
    return make_score100(
        value,
        direction,
        band_for_risk_score(value),
        confidence,
        factors=factors,
        missing_evidence=missing,
        source_lineage=lineage,
        reason=reason,
    )
