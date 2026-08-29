from __future__ import annotations

import dataclasses

import pytest
from server.trust.gate import compute_weight, derive_state
from server.trust.states import (
    CollectorStatus,
    SemanticStatus,
    SourceState,
    SourceTrust,
)

pytestmark = pytest.mark.unit


def _state(collector, semantic, age=0.0, stale_after=300.0, applicable=True):
    return derive_state(collector, semantic, age, stale_after, applicable)


def test_gate_pass_only_for_ok_and_degraded():
    ok = SourceTrust(
        "storage_reliability", SourceState.OK, 1.0, CollectorStatus.OK, SemanticStatus.PLAUSIBLE
    )
    degraded = SourceTrust(
        "free_space", SourceState.DEGRADED, 0.5, CollectorStatus.PARTIAL, SemanticStatus.PLAUSIBLE
    )
    suspect = SourceTrust(
        "throttle", SourceState.SUSPECT, 0.0, CollectorStatus.OK, SemanticStatus.FROZEN
    )
    assert ok.passes_gate is True
    assert degraded.passes_gate is True
    assert suspect.passes_gate is False


def test_source_trust_is_immutable():
    t = SourceTrust("throttle", SourceState.OK, 1.0, CollectorStatus.OK, SemanticStatus.PLAUSIBLE)
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.weight = 0.9  # frozen dataclass -> FrozenInstanceError


def test_not_applicable_wins_over_everything():
    # A sensor genuinely absent from this hardware: not a degradation, the
    # capability simply does not exist on this machine.
    s = _state(CollectorStatus.ABSENT, SemanticStatus.UNCHECKED, applicable=False)
    assert s is SourceState.NOT_APPLICABLE


def test_suspect_beats_collector_ok():
    # A fresh, complete, but lying source is more dangerous than an absent one.
    s = _state(CollectorStatus.OK, SemanticStatus.FROZEN)
    assert s is SourceState.SUSPECT


def test_collector_failure_is_unavailable():
    s = _state(CollectorStatus.BLOCKED, SemanticStatus.PLAUSIBLE)
    assert s is SourceState.UNAVAILABLE


def test_old_sample_is_stale():
    s = _state(CollectorStatus.OK, SemanticStatus.PLAUSIBLE, age=9000.0, stale_after=300.0)
    assert s is SourceState.STALE


def test_partial_payload_is_degraded():
    s = _state(CollectorStatus.PARTIAL, SemanticStatus.PLAUSIBLE)
    assert s is SourceState.DEGRADED


def test_clean_source_is_ok():
    s = _state(CollectorStatus.OK, SemanticStatus.PLAUSIBLE)
    assert s is SourceState.OK


def test_weight_full_for_ok():
    assert compute_weight(SourceState.OK) == 1.0


def test_weight_attenuated_for_degraded():
    assert compute_weight(SourceState.DEGRADED) == 0.5


@pytest.mark.parametrize(
    "state",
    [SourceState.STALE, SourceState.UNAVAILABLE, SourceState.SUSPECT, SourceState.NOT_APPLICABLE],
)
def test_weight_zero_for_gate_fail(state):
    # Hard rule: weight never reanimates a gate-failed source.
    assert compute_weight(state) == 0.0


# --------------------------------------------------------------------------- #
# o5-E1: доменный trust-гейт проведён во ВСЕ движки, не только в сетевой       #
# --------------------------------------------------------------------------- #


def test_storage_risk_withheld_when_domain_untrusted() -> None:
    """o5-E1: гейт домена был проведён только в сетевой движок. Хранилище,
    заполнение диска и деградация ОС считали баллы по данным, которые trust уже
    признал недостоверными -- «лучше UNKNOWN, чем ложная уверенность» нарушено."""
    from server.analytics.storage import compute_storage_risk

    hist = {
        "storage": [
            {
                "disk": "d0",
                "media_type": "SSD",
                "wear_pct": 90,
                "power_on_hours": 40000,
                "read_errors_uncorrected": 5,
            }
        ]
    }
    s = compute_storage_risk(hist, {}, domain_state="unknown")
    assert s.value is None
    assert s.confidence == "unknown"


def test_disk_fill_risk_withheld_when_domain_untrusted() -> None:
    from server.analytics.disk_fill import compute_disk_fill_risk

    series = [{"free_space_pct": 3.0} for _ in range(10)]
    s = compute_disk_fill_risk(series, [], domain_state="unknown")
    assert s.value is None
    assert s.confidence == "unknown"


def test_os_degradation_withheld_when_domain_untrusted() -> None:
    from server.analytics.os_degradation import compute_os_degradation_risk

    hist = {"reliability_stability_index": 2.0, "bugchecks_30d": 5, "app_crashes_30d": 40}
    s = compute_os_degradation_risk(hist, domain_state="unknown")
    assert s.value is None
    assert s.confidence == "unknown"


def test_disk_fill_gate_keeps_servicing_collapse_signal() -> None:
    """Ревью блока E (MEDIUM-3): гейт домена обязан гасить ТОЛЬКО свой сигнал.
    Свободное место приходит из источника free_space, а сбои обслуживания Windows
    — из журнала событий: тревога «машина не обновляется» не должна исчезать
    из-за недоверия к совсем другому источнику."""
    from server.analytics.disk_fill import _SERVICING_MIN_FAILURES, compute_disk_fill_risk

    events = [
        {"source": "Microsoft-Windows-WindowsUpdateClient", "event_id": 20, "level": "Error"}
        for _ in range(_SERVICING_MIN_FAILURES + 1)
    ]
    s = compute_disk_fill_risk([{"free_space_pct": 3.0}] * 10, events, domain_state="unknown")
    assert s.value is not None, "сбой обслуживания погашен чужим гейтом"
    assert any("обновля" in (f.get("label") or "") for f in s.factors)


def test_missing_telemetry_reads_as_no_data_not_failed_gate() -> None:
    """Ревью блока E (LOW-7): у сетевого движка ветка «нет телеметрии» стоит ДО
    доменного гейта намеренно — старый агент должен читать «нет данных», а не
    «источник не прошёл проверку». Три новых движка обязаны вести себя так же."""
    from server.analytics.disk_fill import compute_disk_fill_risk
    from server.analytics.os_degradation import compute_os_degradation_risk
    from server.analytics.storage import compute_storage_risk

    for score in (
        compute_storage_risk({}, {}, domain_state="unknown"),
        compute_disk_fill_risk([], [], domain_state="unknown"),
        compute_os_degradation_risk({}, domain_state="unknown"),
    ):
        assert score.value is None
        assert "гейт" not in score.reason, f"диагностика подменена: {score.reason!r}"


def test_pipeline_wires_each_engine_to_its_own_domain(client, monkeypatch) -> None:
    """Ревью блока E (MEDIUM-6): тесты движков не доказывают, что pipeline передаёт
    ИМЕННО тот домен. Ошибка в имени (os_degradation вместо os_stability) прошла бы
    незамеченной, поэтому недоверие ставим ровно одному домену за раз."""
    from server import db, pipeline
    from server.trust.domains import DOMAIN_SOURCES

    from tests.conftest import envelope, healthy

    engines = {
        "storage": "compute_storage_risk",
        "disk_fill": "compute_disk_fill_risk",
        "os_stability": "compute_os_degradation_risk",
    }
    for key in engines:
        assert key in DOMAIN_SOURCES, f"домен {key} отсутствует в DOMAIN_SOURCES"

    client.post("/api/v1/ingest", json=envelope("e1-dev", "historical", healthy("historical")))

    for untrusted, fn_name in engines.items():
        seen: dict[str, object] = {}
        for key, name in engines.items():
            real = getattr(pipeline, name)

            def _spy(*a, _real=real, _key=key, _seen=seen, **kw):
                _seen[_key] = kw.get("domain_state")
                return _real(*a, **kw)

            monkeypatch.setattr(pipeline, name, _spy)

        domains = {d: {"state": "unknown" if d == untrusted else "trusted"} for d in engines}
        monkeypatch.setattr(db, "get_trust", lambda _did, _d=domains: {"domains": _d})
        pipeline.recompute_scores("e1-dev")

        assert seen[untrusted] == "unknown", f"{fn_name} не получил свой домен {untrusted}"
        others = {k: v for k, v in seen.items() if k != untrusted}
        assert all(v != "unknown" for v in others.values()), (
            f"недоверие к {untrusted} протекло в чужие движки: {others}"
        )
