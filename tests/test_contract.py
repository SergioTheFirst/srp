"""Contract tests: the pydantic message models are the single source of truth."""

from __future__ import annotations

import pytest
from shared.schema import (
    CONTRACT_VERSION,
    Envelope,
    EventBatchPayload,
    HeartbeatPayload,
    HistoricalPayload,
    InventoryPayload,
    SourceHealth,
    StorageReliability,
    parse_payload,
)

from tests.conftest import degrading, healthy

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "msg_type, model",
    [
        ("inventory", InventoryPayload),
        ("historical", HistoricalPayload),
        ("heartbeat", HeartbeatPayload),
        ("events", EventBatchPayload),
    ],
)
def test_parse_payload_returns_typed_model(msg_type, model):
    parsed = parse_payload(msg_type, healthy(msg_type))
    assert isinstance(parsed, model)


def test_parse_payload_unknown_type_raises():
    with pytest.raises(ValueError, match="unknown msg_type"):
        parse_payload("telemetry", {})


def test_payloads_are_forward_compatible():
    """A newer agent may add fields an older server has never seen (extra=allow)."""
    payload = healthy("heartbeat")
    payload["gpu_temp_c"] = 71.0  # field the schema does not define
    parsed = parse_payload("heartbeat", payload)
    # Known field still parses; unknown field is preserved, not rejected.
    assert parsed.cpu_perf_pct == 100.0
    assert parsed.model_dump()["gpu_temp_c"] == 71.0


def test_all_analytic_fields_optional():
    """Missing != zero: an empty payload must validate (sources can be blocked)."""
    parsed = parse_payload("heartbeat", {})
    assert parsed.cpu_pct is None
    assert parsed.free_space_pct is None


def test_nested_models_validate():
    parsed = parse_payload("historical", degrading("historical"))
    assert isinstance(parsed.storage[0], StorageReliability)
    assert parsed.storage[0].wear_pct == 82.0


def test_inventory_uses_cpu_logical_not_threads():
    parsed = parse_payload("inventory", healthy("inventory"))
    assert parsed.cpu_logical == 12
    assert not hasattr(InventoryPayload, "cpu_threads")


def test_envelope_defaults_version_and_timestamp():
    env = Envelope(device_id="dev-1", msg_type="heartbeat", payload={})
    assert env.agent_version == CONTRACT_VERSION
    assert env.ts  # auto-filled ISO timestamp
    assert "T" in env.ts


def test_envelope_agent_version_is_capped():
    """Ревью LOW-1 (2026-08-22): agent_version было единственным полем конверта без
    капа — "0."+"1"*4290+".0" проходила MAJOR-гейт и оседала в devices.agent_version."""
    import pytest as _pytest
    from pydantic import ValidationError

    with _pytest.raises(ValidationError):
        Envelope(
            device_id="d", agent_version="0." + "1" * 40 + ".0", msg_type="heartbeat", payload={}
        )


def test_envelope_rejects_bad_msg_type():
    with pytest.raises(ValueError):
        Envelope(device_id="dev-1", msg_type="nonsense", payload={})


def test_envelope_device_id_at_max_length_is_valid():
    env = Envelope(device_id="d" * 256, msg_type="heartbeat", payload={})
    assert len(env.device_id) == 256


def test_envelope_rejects_oversized_device_id():
    """stoperrors P2-7 defense-in-depth: an oversized device_id could otherwise
    inflate the rate-limiter's in-memory _device_windows dict
    (server/ingest_guards.py) with unboundedly long keys."""
    with pytest.raises(ValueError):
        Envelope(device_id="d" * 257, msg_type="heartbeat", payload={})


# --------------------------------------------------------------------------- #
# SourceHealth and source_health on Envelope (Plan 2)
# --------------------------------------------------------------------------- #


def test_source_health_rejects_unknown_status():
    with pytest.raises(ValueError):
        SourceHealth(status="bogus")


def test_source_health_validates_status():
    sh = SourceHealth(status="ok")
    assert sh.status == "ok"
    assert sh.collected_at is None


def test_source_health_with_collected_at():
    sh = SourceHealth(status="timeout", collected_at="2026-05-30T10:00:00+00:00")
    assert sh.status == "timeout"
    assert sh.collected_at == "2026-05-30T10:00:00+00:00"


def test_source_health_is_forward_compatible():
    """Extra fields on SourceHealth must be tolerated (extra='allow' on _Base)."""
    sh = SourceHealth(status="partial", collected_at=None, future_field="x")
    assert sh.model_dump()["future_field"] == "x"


def test_envelope_default_source_health_is_empty():
    env = Envelope(device_id="dev-1", msg_type="heartbeat", payload={})
    assert env.source_health == {}


def test_envelope_accepts_source_health_block():
    env = Envelope(
        device_id="dev-1",
        msg_type="heartbeat",
        payload={},
        source_health={
            "free_space": {"status": "ok", "collected_at": "2026-05-30T10:00:00+00:00"},
            "throttle": {"status": "timeout", "collected_at": None},
        },
    )
    assert isinstance(env.source_health["free_space"], SourceHealth)
    assert env.source_health["free_space"].status == "ok"
    assert env.source_health["throttle"].status == "timeout"


def test_envelope_source_health_round_trips_through_parse_payload():
    """parse_payload works unaffected; source_health lives on the Envelope only."""
    payload = healthy("heartbeat")
    parsed = parse_payload("heartbeat", payload)
    assert parsed.cpu_perf_pct == 100.0


def test_envelope_without_source_health_still_valid():
    """Old agents that don't send source_health produce a valid Envelope."""
    raw = {
        "device_id": "old-agent",
        "agent_version": CONTRACT_VERSION,
        "msg_type": "heartbeat",
        "payload": {},
    }
    env = Envelope(**raw)
    assert env.source_health == {}


def test_print_jobs_payload_is_capped() -> None:
    """o5-D3: список заданий печати без потолка — неограниченный конверт от агента."""
    import pytest as _pytest
    from pydantic import ValidationError
    from shared.schema import PRINT_JOBS_MAX, PrintJobsPayload

    job = {"job_id": 1, "ts": "2026-03-01T10:00:00+00:00", "printer": "HP", "pages": 1}
    PrintJobsPayload(jobs=[job] * PRINT_JOBS_MAX)  # ровно потолок — принимается
    with _pytest.raises(ValidationError):
        PrintJobsPayload(jobs=[job] * (PRINT_JOBS_MAX + 1))


def test_print_job_strings_are_capped() -> None:
    """LOW-6 (security-ревью 2026-08-21): `printer`/`user_name` без max_length.

    Имя перенаправленной очереди на RDS задаёт непривилегированный пользователь,
    и оно ложится в строки БД — единственные неограниченные строки внешнего
    текста в конверте печати. Кап = 256, симметрично PrinterPortHint.name
    (второй путь того же имени, по нему join printer_ip_map): легитимный агент
    границу не достигает никогда (см. тест ниже), отвергается только прямой
    token-authed постер.
    """
    import pytest as _pytest
    from pydantic import ValidationError
    from shared.schema import PrintJobRecord

    base = {"job_id": 1, "ts": "2026-03-01T10:00:00+00:00", "pages": 1}
    with _pytest.raises(ValidationError):
        PrintJobRecord(**base, printer="p" * 257)
    with _pytest.raises(ValidationError):
        PrintJobRecord(**base, printer="HP", user_name="u" * 257)


def test_print_job_cap_is_above_windows_name_ceiling() -> None:
    """Философия PRINT_JOBS_MAX распространяется на строки: потолок обязан быть
    ВЫШЕ достижимого легитимным агентом, иначе 422 дропает живой конверт = потеря
    заданий. Худшая легальная форма имени — НЕ «220 + « (redirected NNN)»» (суффикс
    RDS добавляется ДО AddPrinter, лимит применяется к финальному имени): длиннее
    всего UNC-имя сетевого подключения `\\\\server\\share`, ограниченное длиной
    имени ключа реестра HKCU\\Printers\\Connections — 255. Запас капа = 1 символ
    (ревью LOW-1, 2026-08-21)."""
    from shared.schema import PrintJobRecord

    worst_legit = "\\\\" + "s" * 120 + "\\" + "p" * 132  # UNC, ровно 255 симв.
    assert len(worst_legit) == 255
    rec = PrintJobRecord(
        job_id=1,
        ts="2026-03-01T10:00:00+00:00",
        printer=worst_legit,
        pages=1,
        user_name="u" * 104,  # потолок UPN-имени в Windows
    )
    assert rec.printer == worst_legit  # принят без изменений — не отвергнут, не обрезан


def test_print_jobs_cap_is_above_agent_byte_budget() -> None:
    """Ревью блока D (H3): агент режет проход по БАЙТАМ (_SWEEP_BUDGET_BYTES), а
    не по числу заданий. Если серверный потолок ниже физически достижимого числа
    записей, конверт получит 422, транспорт его дропнет, а водяной знак уже
    сдвинут — задания печати теряются навсегда."""
    import json

    from client.collectors.print_jobs import _SWEEP_BUDGET_BYTES
    from shared.schema import PRINT_JOBS_MAX

    minimal = {"job_id": 1, "ts": "2026-03-01T10:00:00+00:00", "printer": "p", "pages": 1}
    per_job = len(json.dumps(minimal, ensure_ascii=False).encode("utf-8")) + 1
    max_jobs_possible = _SWEEP_BUDGET_BYTES // per_job
    assert max_jobs_possible <= PRINT_JOBS_MAX, (
        f"серверный потолок {PRINT_JOBS_MAX} ниже достижимых агентом {max_jobs_possible}"
    )
