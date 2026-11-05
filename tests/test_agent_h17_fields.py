"""Agent 1.0.0 (spec H17): the new historical / heartbeat fields, parsed and clipped.

The BugCheck 1001 line «0x0000009f (0x.., 0x.., 0x.., 0x..)» gives a 0x%08x code and up to
four parameters; only the KIND of dump leaves the agent, never its path. Kernel-Power 41
tells a crash (bug check code != 0) from a power loss / a held power button. The agent
clips to the shared.schema caps, so a compliant envelope never meets a 422.
"""

from __future__ import annotations

import pytest
from client.collectors import heartbeat, historical
from client.collectors.ps import PsResult
from pydantic import ValidationError
from shared import schema

from tests.test_collectors_parsers import _HB_FULL, _HIST_BASE, _hist_ps

pytestmark = pytest.mark.unit

LINE = "0x0000009f (0x0000000000000003, 0xffffb08c5c1b7a60, 0xfffff8015e5d6f00, 0xffffb08c5b4ce010)"


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        (r"C:\Windows\MEMORY.DMP", "full"),
        (r"C:\Windows\Minidump\103026-15890-01.dmp", "mini"),
        ("", "none"),
        (r"D:\dumps\crash.dmp", "other"),
    ],
)
def test_a_bugcheck_keeps_the_code_params_and_only_the_dump_kind(path: str, kind: str) -> None:
    item = historical._bugcheck(
        {"ts": "2026-10-30T10:00:00.0000000Z", "raw": LINE, "dumpraw": path}
    )
    assert item["code"] == "0x0000009f"
    assert item["params"] == [
        "0x0000000000000003",
        "0xffffb08c5c1b7a60",
        "0xfffff8015e5d6f00",
        "0xffffb08c5b4ce010",
    ]
    assert item["dump"] == kind
    assert path not in str(item) or not path  # the path never leaves the agent


@pytest.mark.parametrize("raw", ["", "not a bugcheck", "0x1ffffffff (0x1)"])
def test_an_unreadable_bugcheck_line_has_no_code(raw: str) -> None:
    assert historical._bugcheck({"raw": raw})["code"] is None


@pytest.mark.parametrize(
    ("code", "pb", "out"),
    [("0", "0", (0, False)), ("239", "133700000000000000", (239, True)), ("x", None, (None, None))],
)
def test_kp41_tells_a_crash_from_a_power_loss(code, pb, out) -> None:
    item = historical._kp41({"ts": "2026-10-30T10:00:00Z", "code": code, "pb": pb})
    assert (item["bugcheck_code"], item["power_button"]) == out


def test_the_agent_clips_to_the_schema_caps() -> None:
    assert historical._BUGCHECKS_MAX == schema.BUGCHECKS_MAX
    assert historical._KP41_MAX == schema.KP41_MAX
    assert historical._BUGCHECK_PARAMS_MAX == schema.BUGCHECK_PARAMS_MAX
    assert historical._EVENT_TS_MAX == schema.EVENT_TS_MAX
    assert historical._BUGCHECK_PARAM_MAX == schema.BUGCHECK_PARAM_MAX


def test_a_full_historical_payload_parses_against_the_contract(monkeypatch) -> None:
    raw = {
        **_HIST_BASE,
        "storage": [],
        "last_boot_ms": 70451,
        "bugchecks": [{"ts": "2026-10-30T10:00:00Z", "raw": LINE, "dumpraw": ""}] * 25,
        "kp41": [{"ts": "x" * 60, "code": "0", "pb": "0"}] * 25,
    }
    monkeypatch.setattr(historical, "run_ps", _hist_ps(raw))
    payload = historical.collect_historical().payload
    assert len(payload["bugchecks"]) == 20 and len(payload["kp41"]) == 20
    parsed = schema.HistoricalPayload.model_validate(payload)  # no 422 from a compliant agent
    assert parsed.last_boot_ms == 70451 and parsed.kp41[0].ts == "x" * 40


def test_the_schema_rejects_what_the_agent_never_sends() -> None:
    with pytest.raises(ValidationError):
        schema.HistoricalPayload.model_validate({"bugchecks": [{"code": "0x9f"}] * 21})
    with pytest.raises(ValidationError):
        schema.HistoricalPayload.model_validate({"bugchecks": [{"params": ["0x" + "f" * 17]}]})


def test_the_heartbeat_reports_free_gigabytes(monkeypatch) -> None:
    monkeypatch.setattr(
        heartbeat, "run_ps", lambda *a, **k: PsResult("ok", {**_HB_FULL, "free_space_gb": 35.41})
    )
    payload = heartbeat.collect_heartbeat().payload
    assert payload["free_space_gb"] == 35.41
    assert schema.HeartbeatPayload.model_validate(payload).free_space_gb == 35.41
