"""Tray certificate reminder engine (tray spec §3).

Pure, off-Windows tests for ``client.tray.certs``: PS-JSON parsing, Subject-CN
grouping (a valid successor silences the old cert before *and* after expiry),
the nag schedule (>14d silence / 8-14d & <=7d every 4h / expired <=7d once per
calendar day / expired >7d silent but red), the ``tray_require_cert`` empty-store
case, and the privacy/language-independence pins on the PowerShell snippet.

Determinism: calendar-day behaviour is anchored with ``time.mktime`` (local
epoch) so "next day" is a real local-date change regardless of the test box TZ.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest
from client.collectors.ps import PsResult
from client.tray import certs as cz

_DAY = 86_400


def _epoch(year: int, month: int, day: int, hour: int = 12) -> float:
    """Local-time epoch for a wall-clock date (DST resolved by mktime)."""
    return time.mktime((year, month, day, hour, 0, 0, 0, 0, -1))


def _cert_dict(
    *,
    cn: str = "Иванов Иван Иванович",
    thumbprint: str = "AA11",
    not_after: float = 0.0,
    not_before: float = 0.0,
    has_private_key: bool = True,
) -> dict[str, Any]:
    return {
        "subject": f"CN={cn}, O=ООО Ромашка, C=RU",
        "issuer": "CN=Test CA",
        "thumbprint": thumbprint,
        "not_after": int(not_after),
        "not_before": int(not_before),
        "has_private_key": has_private_key,
    }


def _cert(**kw: Any) -> cz.CertInfo:
    d = _cert_dict(**kw)
    return cz.CertInfo(
        subject=d["subject"],
        issuer=d["issuer"],
        thumbprint=d["thumbprint"],
        not_before=d["not_before"],
        not_after=d["not_after"],
        has_private_key=d["has_private_key"],
    )


NOW = _epoch(2026, 6, 13)


# --------------------------------------------------------------------------- #
# parse_certs
# --------------------------------------------------------------------------- #


def test_parse_certs_accepts_single_object() -> None:
    """ConvertTo-Json emits a bare object for one cert; parse normalises to a list."""
    raw = json.dumps(_cert_dict(thumbprint="DEAD", not_after=NOW + 5 * _DAY))
    certs = cz.parse_certs(raw)
    assert len(certs) == 1
    assert certs[0].thumbprint == "DEAD"
    assert certs[0].not_after == int(NOW + 5 * _DAY)


def test_parse_certs_accepts_list_and_coerces_epoch() -> None:
    raw = json.dumps([_cert_dict(thumbprint="A"), _cert_dict(thumbprint="B")])
    certs = cz.parse_certs(raw)
    assert [c.thumbprint for c in certs] == ["A", "B"]
    assert all(isinstance(c.not_after, int) for c in certs)


def test_parse_certs_keeps_russian_subject() -> None:
    raw = json.dumps(_cert_dict(cn="Пётр Петров"))
    (cert,) = cz.parse_certs(raw)
    assert "Пётр Петров" in cert.subject


def test_parse_certs_bad_json_is_empty() -> None:
    assert cz.parse_certs("not json at all") == []
    assert cz.parse_certs("") == []


def test_parse_certs_skips_unusable_entries() -> None:
    """A row missing a thumbprint or with a non-numeric date is dropped, not fatal."""
    raw = json.dumps(
        [
            _cert_dict(thumbprint="GOOD", not_after=NOW + _DAY),
            {"subject": "CN=x", "issuer": "y"},  # no thumbprint / dates
            {"thumbprint": "Z", "not_after": "soon"},  # non-numeric date
        ]
    )
    certs = cz.parse_certs(raw)
    assert [c.thumbprint for c in certs] == ["GOOD"]


# --------------------------------------------------------------------------- #
# subject_cn + grouping
# --------------------------------------------------------------------------- #


def test_subject_cn_extracts_and_normalises() -> None:
    assert cz.subject_cn("CN=Иванов Иван, O=Org, C=RU") == "иванов иван"
    assert cz.subject_cn("CN=Foo") == "foo"


def test_subject_cn_falls_back_to_whole_subject() -> None:
    # No CN component -> use the whole (normalised) subject so it still groups.
    assert cz.subject_cn("O=Bar, C=RU") == "o=bar, c=ru"


def test_group_by_subject_groups_same_cn_excludes_keyless() -> None:
    same_a = _cert(cn="Роль А", thumbprint="A1", not_after=NOW + 10 * _DAY)
    same_b = _cert(cn="Роль А", thumbprint="A2", not_after=NOW + 400 * _DAY)
    keyless = _cert(cn="Роль А", thumbprint="A3", has_private_key=False)
    other = _cert(cn="Роль Б", thumbprint="B1", not_after=NOW + 5 * _DAY)
    groups = cz.group_by_subject([same_a, same_b, keyless, other])
    assert set(groups) == {"роль а", "роль б"}
    assert {c.thumbprint for c in groups["роль а"]} == {"A1", "A2"}  # keyless dropped


def test_best_cert_is_latest_not_after() -> None:
    old = _cert(thumbprint="OLD", not_after=NOW)
    new = _cert(thumbprint="NEW", not_after=NOW + 400 * _DAY)
    assert cz.best_cert([old, new]).thumbprint == "NEW"


# --------------------------------------------------------------------------- #
# cert_level_for (icon colour bands)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "days, expected",
    [
        (20, "ok"),
        (14, "warn"),  # 8-14 inclusive of 14
        (8, "warn"),
        (7, "alert"),  # <=7 is red
        (5, "alert"),
        (-1, "alert"),  # expired
    ],
)
def test_cert_level_bands(days: int, expected: str) -> None:
    best = _cert(not_after=NOW + days * _DAY)
    assert cz.cert_level_for(best, now=NOW, warn_days=14) == expected


# --------------------------------------------------------------------------- #
# should_nag (pure schedule)
# --------------------------------------------------------------------------- #


def test_should_nag_4h_cadence_boundary() -> None:
    fire, st1 = cz.should_nag("warn", {}, now=NOW, notify_hours=4, today="2026-06-13")
    assert fire is True and st1["last_nag_epoch"] == NOW

    fire3, _ = cz.should_nag(
        "warn", {"last_nag_epoch": NOW - 3 * 3600}, now=NOW, notify_hours=4, today="d"
    )
    assert fire3 is False  # only 3h elapsed

    fire4, _ = cz.should_nag(
        "warn", {"last_nag_epoch": NOW - 4 * 3600}, now=NOW, notify_hours=4, today="d"
    )
    assert fire4 is True  # exactly 4h -> nag


def test_should_nag_expired_once_per_calendar_day() -> None:
    first, st1 = cz.should_nag("expired_recent", {}, now=NOW, notify_hours=4, today="2026-06-13")
    assert first is True and st1["last_nag_date"] == "2026-06-13"

    same, _ = cz.should_nag(
        "expired_recent",
        {"last_nag_date": "2026-06-13"},
        now=NOW + 3600,
        notify_hours=4,
        today="2026-06-13",
    )
    assert same is False  # same calendar day

    nextday, _ = cz.should_nag(
        "expired_recent",
        {"last_nag_date": "2026-06-13"},
        now=NOW + _DAY,
        notify_hours=4,
        today="2026-06-14",
    )
    assert nextday is True


def test_should_nag_silent_categories() -> None:
    assert cz.should_nag("ok", {}, now=NOW, notify_hours=4, today="d")[0] is False
    assert cz.should_nag("expired_old", {}, now=NOW, notify_hours=4, today="d")[0] is False


# --------------------------------------------------------------------------- #
# evaluate -- end to end behaviour
# --------------------------------------------------------------------------- #


def test_evaluate_none_is_unknown_and_keeps_state() -> None:
    """A PowerShell failure must not look like an expired cert (UNKNOWN over red)."""
    res = cz.evaluate(None, {"AA11": {"last_nag_epoch": 1.0}}, now=NOW)
    assert res.level == "unknown"
    assert res.balloons == ()
    assert res.state == {"AA11": {"last_nag_epoch": 1.0}}  # untouched
    assert "проверить" in res.panel_text


def test_evaluate_healthy_cert_is_silent_green() -> None:
    good = _cert(not_after=NOW + 200 * _DAY)
    res = cz.evaluate([good], {}, now=NOW)
    assert res.level == "ok"
    assert res.balloons == ()
    assert "действует до" in res.panel_text


def test_evaluate_warn_band_nags_every_4h_with_helpdesk() -> None:
    cert = _cert(not_after=NOW + 10 * _DAY)
    r1 = cz.evaluate([cert], {}, now=NOW, helpdesk="ИТ: 1234")
    assert r1.level == "warn"
    assert len(r1.balloons) == 1
    assert "через 10 дн" in r1.balloons[0].message
    assert "ИТ: 1234" in r1.balloons[0].message
    assert r1.balloons[0].level == "warn"

    # within 4h -> no second balloon, icon still warn
    r2 = cz.evaluate([cert], r1.state, now=NOW + 3 * 3600)
    assert r2.balloons == () and r2.level == "warn"

    # at 4h -> nag again
    r3 = cz.evaluate([cert], r2.state, now=NOW + 4 * 3600)
    assert len(r3.balloons) == 1


def test_evaluate_critical_band_is_red() -> None:
    cert = _cert(not_after=NOW + 5 * _DAY)
    res = cz.evaluate([cert], {}, now=NOW)
    assert res.level == "alert"
    assert res.balloons[0].level == "alert"


def test_evaluate_expired_recent_once_per_day_then_silent_when_old() -> None:
    recent = _cert(thumbprint="EXP", not_after=_epoch(2026, 6, 10))  # 3 days ago
    r1 = cz.evaluate([recent], {}, now=NOW)
    assert r1.level == "alert"
    assert len(r1.balloons) == 1
    assert "ИСТЁК" in r1.balloons[0].message

    r_same = cz.evaluate([recent], r1.state, now=NOW + 3600)
    assert r_same.balloons == ()  # same calendar day

    r_next = cz.evaluate([recent], r1.state, now=_epoch(2026, 6, 14))
    assert len(r_next.balloons) == 1  # new day, still <=7d expired

    old = _cert(thumbprint="EXP", not_after=_epoch(2026, 6, 1))  # 12 days ago
    r_old = cz.evaluate([old], {}, now=NOW)
    assert r_old.level == "alert"  # icon stays red
    assert r_old.balloons == ()  # ...but balloons stop after 7 days


def test_evaluate_successor_silences_old_and_announces_once() -> None:
    old = _cert(cn="Иванов", thumbprint="OLD", not_after=_epoch(2026, 5, 1))  # expired
    new = _cert(cn="Иванов", thumbprint="NEW", not_after=NOW + 300 * _DAY)  # fresh
    r1 = cz.evaluate([old, new], {}, now=NOW)
    assert r1.level == "ok"  # the valid successor drives the group
    assert len(r1.balloons) == 1
    assert "новый сертификат" in r1.balloons[0].message.lower()
    assert r1.balloons[0].level == "ok"  # informational

    r2 = cz.evaluate([old, new], r1.state, now=NOW + 3600)
    assert r2.balloons == ()  # announced only once, old stays silenced


def test_evaluate_distinct_subjects_are_independent() -> None:
    expiring = _cert(cn="Роль А", thumbprint="A", not_after=NOW + 5 * _DAY)
    healthy = _cert(cn="Роль Б", thumbprint="B", not_after=NOW + 300 * _DAY)
    res = cz.evaluate([expiring, healthy], {}, now=NOW)
    assert res.level == "alert"  # worst-of
    assert len(res.balloons) == 1  # only Role A nags; Role B silent


def test_evaluate_require_cert_empty_store_daily() -> None:
    r1 = cz.evaluate([], {}, now=NOW, require_cert=True, helpdesk="ИТ")
    assert r1.level == "alert"
    assert len(r1.balloons) == 1
    assert "не установлен" in r1.balloons[0].message

    r_same = cz.evaluate([], r1.state, now=NOW + 3600, require_cert=True)
    assert r_same.balloons == ()

    r_next = cz.evaluate([], r1.state, now=_epoch(2026, 6, 14), require_cert=True)
    assert len(r_next.balloons) == 1


def test_evaluate_empty_store_not_required_is_ok() -> None:
    res = cz.evaluate([], {}, now=NOW, require_cert=False)
    assert res.level == "ok"
    assert res.balloons == ()


def test_evaluate_panel_text_expired_format() -> None:
    expired = _cert(not_after=_epoch(2026, 6, 3))
    res = cz.evaluate([expired], {}, now=NOW)
    assert res.panel_text.startswith("истёк")
    assert "03.06.2026" in res.panel_text


def test_evaluate_does_not_mutate_input_state() -> None:
    cert = _cert(not_after=NOW + 5 * _DAY)
    state: dict[str, Any] = {}
    cz.evaluate([cert], state, now=NOW)
    assert state == {}  # immutable: a fresh dict is returned


def test_evaluate_prunes_stale_thumbprints() -> None:
    """Thumbprints no longer in the store are dropped (no unbounded growth)."""
    cert = _cert(thumbprint="CURRENT", not_after=NOW + 5 * _DAY)
    stale = {"GONE": {"last_nag_epoch": 1.0}, "ALSOGONE": {"new_cert_announced": True}}
    res = cz.evaluate([cert], stale, now=NOW)
    assert set(res.state) == {"CURRENT"}


def test_evaluate_empty_store_prunes_old_thumbprints() -> None:
    assert cz.evaluate([], {"OLD": {"x": 1}}, now=NOW, require_cert=False).state == {}
    res = cz.evaluate([], {"OLD": {"x": 1}}, now=NOW, require_cert=True)
    assert set(res.state) == {"_missing"}


# --------------------------------------------------------------------------- #
# query_certs adapter (PsResult -> Optional[list])
# --------------------------------------------------------------------------- #


def _fake_ps(result: PsResult) -> Any:
    def _run(_script: str, timeout: int = 30) -> PsResult:
        return result

    return _run


def test_query_certs_ok_returns_list() -> None:
    data = [_cert_dict(thumbprint="A", not_after=NOW + _DAY)]
    certs = cz.query_certs(run_ps_fn=_fake_ps(PsResult("ok", data)))
    assert certs is not None and [c.thumbprint for c in certs] == ["A"]


def test_query_certs_empty_store_is_empty_list_not_none() -> None:
    # An empty personal store is a *fact* (green/normal), not an unknown.
    assert cz.query_certs(run_ps_fn=_fake_ps(PsResult("empty"))) == []


@pytest.mark.parametrize("status", ["timeout", "blocked", "absent", "partial"])
def test_query_certs_ps_failure_is_unknown(status: str) -> None:
    assert cz.query_certs(run_ps_fn=_fake_ps(PsResult(status))) is None


# --------------------------------------------------------------------------- #
# privacy + language-independence pins on the PowerShell snippet
# --------------------------------------------------------------------------- #


def test_ps_script_never_reads_private_keys() -> None:
    script = cz._CERT_SCRIPT
    assert "HasPrivateKey" in script  # we filter on the boolean...
    # ...but never touch the key material itself.
    for forbidden in ("Export", "PrivateKey.", "RSACertificate", "GetRSAPrivateKey", ".Export("):
        assert forbidden not in script


def test_ps_script_uses_epoch_not_localised_dates() -> None:
    script = cz._CERT_SCRIPT
    assert "ToUnixTimeSeconds" in script
    assert "ToString(" not in script  # no localized date formatting
    assert "CurrentUser\\My" in script  # the user's personal store only


def test_ps_script_caps_cert_count() -> None:
    # A pathological store (thousands of certs) must not balloon PS stdout.
    assert "Select-Object -First" in cz._CERT_SCRIPT


def test_ps_script_is_powershell_51_safe() -> None:
    script = cz._CERT_SCRIPT
    # PS 6+ only / unsafe constructs must not appear (agent-powershell-51-floor).
    for forbidden in ("??", "ForEach-Object -Parallel", "ConvertFrom-Json -AsHashtable"):
        assert forbidden not in script
