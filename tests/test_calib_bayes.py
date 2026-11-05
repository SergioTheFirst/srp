"""D10 calibration of the Bayesian prioritizer (server/scoring/bayesian.py).

Spec: docs/superpowers/specs/2026-10-09-health-calibration-design.md, D10.
  - SSD wear enters only past 70 % and is capped at 1.5 log-odds: wear alone is
    never 'critical'.
  - Throttle and the KP41 anchor need a busy CPU (cpu_pct >= 50): idle SpeedStep
    is not throttling, and 6008 cannot anchor KP41 (same unclean shutdown).
  - With an OS-engine value the stability class does not re-add RSI and BSOD.
  - Uncorrected I/O errors are preferred over the ECC-inclusive totals.
H10 (spec 2026-10-30): with a storage-engine value the raw SMART factors are not
re-added (counted once); age and power-on hours are one context factor <= 0.4; the
storage class never ranks below the verdict's storage rung (h2/h3/h4 floor).
"""

from __future__ import annotations

import math
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from server.analytics.health import compute_health
from server.analytics.health_dominant import storage_rung
from server.analytics.storage import compute_storage_risk
from server.analytics.trends import _domain_risk
from server.pipeline import _storage_axis
from server.scoring.bayesian import compute_risk

pytestmark = pytest.mark.unit


def _class(r: dict[str, Any], name: str) -> dict[str, Any]:
    return next(c for c in r["classes"] if c["name"] == name)


def _labels(r: dict[str, Any], name: str) -> list[str]:
    return [f["label"] for f in _class(r, name)["factors"]]


def _disk(**fields: Any) -> dict[str, Any]:
    return {"storage": [{"disk": "d0", "media_type": "SSD", **fields}]}


# --------------------------------------------------------------------------- #
# Wear: starts at 70 %, capped at 1.5 log-odds
# --------------------------------------------------------------------------- #
def test_wear_below_70_adds_no_weight():
    r = compute_risk(None, _disk(wear_pct=60), None)
    assert not any("Износ SSD" in ln for ln in _labels(r, "storage"))


@pytest.mark.parametrize(("wear", "weight"), [(85, 0.75), (100, 1.5), (140, 1.5)])
def test_wear_weight_ramps_from_70_and_caps(wear, weight):
    r = compute_risk(None, _disk(wear_pct=wear), None)
    factor = next(f for f in _class(r, "storage")["factors"] if "Износ SSD" in f["label"])
    assert factor["weight"] == pytest.approx(weight)


def test_wear_alone_is_never_critical():
    """Wear 100 %, no age/inventory. H10 counts the SMART reading once: with its engine
    term (storage_risk 80) only the engine's 0.8 (p ~ 0.10), without an engine value
    the raw wear term (1.5, p ~ 0.19) -- 'elevated' both ways, never 'critical'. How far
    above that it ranks is the verdict's storage rung (the floor), not the wear."""
    with_engine = compute_risk(
        None, _disk(wear_pct=100), None, domain_values={"storage_risk": 80.0}
    )
    raw = compute_risk(None, _disk(wear_pct=100), None)
    assert _class(with_engine, "storage")["level"] == _class(raw, "storage")["level"] == "elevated"
    assert not any("Износ SSD" in ln for ln in _labels(with_engine, "storage"))


# --------------------------------------------------------------------------- #
# Throttle and KP41: only under load
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "hb",
    [
        {"cpu_pct": 5.0, "cpu_perf_pct": 55.0},
        {"cpu_pct": 5.0, "cpu_perf_pct": 90.0},
        {"cpu_pct": 49.9, "cpu_perf_pct": 55.0},  # just below the scores.BUSY_CPU_PCT gate
        {"cpu_perf_pct": 55.0},  # load unknown (no cpu_pct) -> not busy
    ],
)
def test_idle_low_perf_is_not_throttling(hb):
    r = compute_risk(None, None, hb)
    assert not any("троттлинг" in ln.lower() for ln in _labels(r, "power_thermal"))


def test_kp41_not_anchored_by_dirty_shutdown_alone():
    hist = {"dirty_shutdowns_30d": 2, "kernel_power_41_30d": 2}
    r = compute_risk(None, hist, {"cpu_pct": 5.0, "cpu_perf_pct": 100.0})
    labels = _labels(r, "power_thermal")
    assert not any("KP41" in ln for ln in labels)
    assert any("(6008)" in ln for ln in labels)  # the dirty shutdown itself still counts


def test_light_throttle_under_load_counts_but_does_not_anchor_kp41():
    r = compute_risk(None, {"kernel_power_41_30d": 3}, {"cpu_pct": 80.0, "cpu_perf_pct": 90.0})
    labels = _labels(r, "power_thermal")
    assert any("Лёгкий троттлинг CPU" in ln for ln in labels)
    assert not any("KP41" in ln for ln in labels)


@pytest.mark.parametrize("cpu_pct", [50.0, 80.0])
def test_kp41_anchored_by_throttle_under_load(cpu_pct):
    hist = {"kernel_power_41_30d": 3}
    r = compute_risk(None, hist, {"cpu_pct": cpu_pct, "cpu_perf_pct": 70.0})
    labels = _labels(r, "power_thermal")
    assert any("Троттлинг CPU" in ln for ln in labels)
    assert any("KP41" in ln for ln in labels)


# --------------------------------------------------------------------------- #
# Stability: RSI and BSOD counted once
# --------------------------------------------------------------------------- #
_STAB_HIST = {"reliability_stability_index": 3.0, "bugchecks_30d": 3}


def test_stability_counts_rsi_and_bsod_once_with_engine():
    hist = {**_STAB_HIST, "app_crashes_30d": 8}
    inv = {"pending_reboot": True, "driver_problem_count": 2}
    r = compute_risk(inv, hist, None, domain_values={"os_degradation_risk": 60.0})
    labels = _labels(r, "stability")
    # only RSI/BSOD are covered by the OS engine -- the other direct factors stay
    assert any("падение приложений" in ln for ln in labels)
    assert any("Ожидается перезагрузка" in ln for ln in labels)
    assert any("ошибкой драйвера" in ln for ln in labels)
    assert not any("индекс стабильности" in ln for ln in labels)
    assert not any("BSOD (30д)" in ln for ln in labels)
    assert any("Движок деградации OS" in ln for ln in labels)


@pytest.mark.parametrize(
    "domain_values",
    [
        None,
        # pipeline always passes the four engine keys; a None value is UNKNOWN
        {"storage_risk": 40.0, "os_degradation_risk": None},
        # non-finite is UNKNOWN for _domain_lo too: the engine adds nothing
        {"os_degradation_risk": math.nan},
    ],
)
def test_stability_fallback_without_engine_keeps_direct_factors(domain_values):
    r = compute_risk(None, _STAB_HIST, None, domain_values=domain_values)
    labels = _labels(r, "stability")
    assert any("индекс стабильности" in ln for ln in labels)
    assert any("BSOD (30д)" in ln for ln in labels)


# --------------------------------------------------------------------------- #
# I/O errors: uncorrected preferred, totals only for legacy agents
# --------------------------------------------------------------------------- #
def test_io_errors_use_uncorrected_when_present():
    totals = {"read_errors_total": 5000, "write_errors_total": 0}
    ecc_only = _disk(**totals, read_errors_uncorrected=0, write_errors_uncorrected=0)
    labels = _labels(compute_risk(None, ecc_only, None), "storage")
    assert not any("Накопленные ошибки I/O" in ln for ln in labels)

    legacy = _labels(compute_risk(None, _disk(**totals), None), "storage")
    assert "Накопленные ошибки I/O: 5000" in legacy

    partial = _disk(**totals, read_errors_uncorrected=3)  # write side not reported
    assert "Накопленные ошибки I/O: 3" in _labels(compute_risk(None, partial, None), "storage")


# --------------------------------------------------------------------------- #
# H10: the ranker agrees with the verdict
# --------------------------------------------------------------------------- #
_LEVEL_RANK = {"low": 0, "elevated": 1, "high": 2, "critical": 3}
_FLOOR_LEVEL = {"h2": "elevated", "h3": "high", "h4": "critical"}


@pytest.mark.parametrize("engine", [0.0, 60.0])
def test_an_engine_value_counts_the_smart_reading_once(engine: float) -> None:
    hist = _disk(wear_pct=95, read_errors_uncorrected=3)
    labels = _labels(
        compute_risk(None, hist, None, domain_values={"storage_risk": engine}), "storage"
    )
    assert not any("Износ SSD" in ln or "ошибки I/O" in ln for ln in labels)
    fallback = _labels(
        compute_risk(None, hist, None, domain_values={"storage_risk": None}), "storage"
    )
    assert any("Износ SSD" in ln for ln in fallback) and any("ошибки I/O" in ln for ln in fallback)


def test_age_and_power_on_hours_are_one_context_factor_of_at_most_0_4() -> None:
    hist = _disk(power_on_hours=60_000)  # ~6.8 years of power-on time
    inv = {"bios_release_date": "2010-01-01"}  # a 16-year-old machine
    storage = _class(compute_risk(inv, hist, None), "storage")
    context = [f for f in storage["factors"] if "наработка диска" in f["label"]]
    assert len(context) == 1 and context[0]["weight"] == pytest.approx(0.4)
    assert not any(f["label"].startswith("Наработка диска") for f in storage["factors"])


def test_the_dead_reallocated_sectors_factor_is_gone() -> None:
    labels = _labels(compute_risk(None, _disk(reallocated_sectors=500), None), "storage")
    assert not any("Переназначено секторов" in ln for ln in labels)


@pytest.mark.parametrize(
    ("rung", "level"), [("h2", "elevated"), ("h3", "high"), ("h4", "critical")]
)
def test_the_storage_class_never_ranks_below_the_verdicts_storage_rung(
    rung: str, level: str
) -> None:
    r = compute_risk(
        None, None, None, domain_values={"storage_risk": 0.0}, storage_floor_state=rung
    )
    storage = _class(r, "storage")
    assert storage["level"] == level
    assert sum(f["weight"] for f in storage["factors"]) == pytest.approx(
        math.log(storage["probability"] / (1 - storage["probability"])), abs=0.02
    )  # the lift is a labelled term: the explanation still sums to the posterior


@pytest.mark.parametrize("rung", [None, "h0", "h1", "h3"])
def test_the_floor_never_lowers_a_class(rung) -> None:
    hist = _disk(wear_pct=100, read_errors_uncorrected=5)  # raw p ~ 0.77: critical
    assert (
        _class(compute_risk(None, hist, None, storage_floor_state=rung), "storage")["level"]
        == "critical"
    )


def test_spare_running_out_reaches_the_rung_through_the_trajectory_share() -> None:
    axis = {"value": 10.0, "coords": {"damage": 0.0, "resilience_loss": 0.0, "flags": []}}
    spare = {"nvme_spare": {"direction": "worsening", "eta_days": 20.0}}
    assert storage_rung(axis, spare) == "h3"  # the flag alone (spare_depleting) is only h2
    assert storage_rung(axis, {}) == "h0"
    assert storage_rung({"value": None}, spare) is None
    fs = {
        "value": 10.0,
        "coords": {"damage": 0.0, "resilience_loss": 0.0, "flags": ["fs_corruption"]},
    }
    assert storage_rung(fs, {}) == "h0"  # H6: the file system, not the disk


def _healthy_others() -> dict:
    zero = {"value": 0.0, "band": "good", "confidence": "high", "factors": []}
    return {"os_degradation_risk": zero, "software_aging_risk": {**zero, "coords": {"flags": []}}}


@pytest.mark.parametrize(
    "disk",
    [
        {"smart_predict_fail": True},  # the audit's case: a predict_fail h4 ranked «low»
        {"smart_attrs": {"197": 12, "198": 3}},  # two hard media flags: h4
        {"smart_attrs": {"197": 12}},  # pending > 10: h3
        {"smart_attrs": {"5": 2}},  # damage present: h1 -- no floor
        {"wear_pct": 3},  # healthy
    ],
)
def test_the_storage_level_follows_the_verdict_state_of_a_storage_only_device(disk: dict) -> None:
    row = {"disk": "SSD", "serial_hash": "s1", "media_type": "SSD", "bus_type": "SATA", **disk}
    risk = compute_storage_risk({"storage": [row]}, None)
    axis = _storage_axis(risk, None)
    rung = storage_rung(axis, {})
    verdict = compute_health({"storage_risk": axis, **_healthy_others()}, {}, {}, None, None, None)
    assert verdict.raw_state == rung  # the storage alone sets this verdict
    level = _class(
        compute_risk(
            None, None, None, domain_values={"storage_risk": risk.value}, storage_floor_state=rung
        ),
        "storage",
    )["level"]
    assert _LEVEL_RANK[level] >= _LEVEL_RANK[_FLOOR_LEVEL.get(rung, "low")], (disk, rung, level)


def test_the_storage_level_never_drops_as_the_storage_rung_rises() -> None:
    """H10 property on a grid of storage axes and spare forecasts: the class level is
    at least its rung's floor, so it never falls while the rung climbs."""
    for value in (0.0, 30.0, 70.0, 100.0):
        for flags in (
            [],
            ["remap_masking"],
            ["pending_gt10"],
            ["predict_fail"],
            ["damage_present"],
        ):
            for rloss in (0.0, 45.0, 75.0):
                for eta in (None, 60.0, 20.0):
                    damage = 45.0 if "damage_present" in flags else 0.0
                    axis = {
                        "value": value,
                        "coords": {"damage": damage, "resilience_loss": rloss, "flags": flags},
                    }
                    trends = (
                        {}
                        if eta is None
                        else {"nvme_spare": {"direction": "worsening", "eta_days": eta}}
                    )
                    rung = storage_rung(axis, trends)
                    r = compute_risk(
                        None,
                        None,
                        None,
                        domain_values={"storage_risk": value},
                        storage_floor_state=rung,
                    )
                    level = _class(r, "storage")["level"]
                    assert _LEVEL_RANK[level] >= _LEVEL_RANK[_FLOOR_LEVEL.get(rung, "low")], (
                        axis,
                        eta,
                    )
    assert _domain_risk("nvme_spare", 20.0) * 0.85 >= 70.0  # why ETA 20 reaches h3 above


def test_an_engine_value_does_not_re_add_power_on_hours() -> None:
    """The engine scores POH itself (storage.py «Power-on» rule): with its value, POH is
    not re-added through the age context factor (H10 lists POH among raw SMART)."""
    dv = {"storage_risk": 45.0}
    worn, fresh = (
        _class(compute_risk(None, _disk(power_on_hours=h), None, domain_values=dv), "storage")
        for h in (60_000, 0)
    )
    assert worn["probability"] == fresh["probability"] and worn["level"] == "low"
    assert not any("наработка диска" in f["label"] for f in worn["factors"])
    inv = {"bios_release_date": "2010-01-01"}  # device age itself is still context
    aged = _class(
        compute_risk(inv, _disk(power_on_hours=60_000), None, domain_values=dv), "storage"
    )
    assert any(f["label"].startswith("Возраст оборудования ~") for f in aged["factors"])


@pytest.mark.parametrize(
    "later",
    [
        {"smart_attrs": {"197": 0, "198": 0}},  # the flags lapse: a release window
        {"wear_pct": 5},  # the SMART script timed out: blind «loss», a hold that never ends
    ],
)
def test_a_held_storage_level_is_not_floored_only_this_cycles_rung(later: dict) -> None:
    """H10 floors at THIS cycle's storage rung, not at a held level: the verdict keeps h4
    (health_hold) while the class reads the fresh evidence -- the documented boundary."""
    row = {"disk": "SSD", "serial_hash": "s1", "media_type": "SSD", "bus_type": "SATA"}
    t0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
    prev = None
    for hours, extra in ((0, {"smart_attrs": {"197": 12, "198": 3}}), (24, later)):
        risk = compute_storage_risk({"storage": [{**row, **extra}]}, None)
        axis = _storage_axis(risk, None)
        verdict = compute_health(
            {"storage_risk": axis, **_healthy_others()},
            {},
            {},
            None,
            None,
            prev,
            now=t0 + timedelta(hours=hours),
        )
        prev = asdict(verdict)
    assert (verdict.state, verdict.rung_source, verdict.dominant) == ("h4", "hold", "storage")
    rung = storage_rung(axis, {})
    assert rung == "h0"
    r = compute_risk(
        None, None, None, domain_values={"storage_risk": risk.value}, storage_floor_state=rung
    )
    assert _class(r, "storage")["level"] == "low"
