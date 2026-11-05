"""Dashboard surfacing of telemetry trust (Plan 3d).

Renders the device page through TestClient and checks the trust is visible: an
untrusted domain shows UNKNOWN (not a false-confidence %), an untrusted identity
shows a banner, and a coverage section lists the domains.
"""

from __future__ import annotations

import pytest

from tests.conftest import healthy

pytestmark = pytest.mark.integration


def _sh(status: str) -> dict:
    return {"status": status, "collected_at": "2026-05-30T00:00:00+00:00"}


def _env(device_id: str, msg_type: str, payload: dict, source_health: dict) -> dict:
    return {
        "device_id": device_id,
        "agent_version": "0.1.0",
        "msg_type": msg_type,
        "payload": payload,
        "source_health": source_health,
    }


def test_device_page_marks_unknown_domain(client):
    sh = {
        "storage_reliability": _sh("blocked"),
        "reliability": _sh("ok"),
        "boot_time": _sh("ok"),
    }
    client.post("/api/v1/ingest", json=_env("dx", "historical", healthy("historical"), sh))
    resp = client.get("/device/dx")
    assert resp.status_code == 200
    assert "UNKNOWN" in resp.text
    assert "Покрытие источников" in resp.text


def test_device_page_shows_untrusted_banner(client):
    client.post(
        "/api/v1/ingest",
        json=_env("du", "inventory", healthy("inventory"), {"identity": _sh("blocked")}),
    )
    resp = client.get("/device/du")
    assert resp.status_code == 200
    assert "Идентичность недостоверна" in resp.text


@pytest.mark.parametrize(
    ("state", "collector", "fragment"),
    [
        ("stale", "ok", "нет свежей инвентаризации"),
        ("unavailable", "timeout", "не уложилась"),
        ("unavailable", "empty", "PowerShell ничего не вернул"),
        ("unavailable", "blocked", "PowerShell для инвентаризации запрещён"),
        ("unavailable", "absent", "PowerShell не найден"),
        ("suspect", "ok", "противоречив"),
    ],
)
def test_identity_reason_names_the_cause_not_the_token(state, collector, fragment):
    """2026-10-14: владелец читал «untrusted» как проблему токена. Каждая
    причина называет реальный сбой и прямо говорит, что токен ни при чём."""
    from server.web.trust_labels import identity_reason_ru

    text = identity_reason_ru({"state": state, "collector_status": collector})
    assert fragment in text
    assert text.endswith("С токеном не связано.")


@pytest.mark.parametrize("raw", [None, {}, {"state": "weird", "collector_status": "x"}])
def test_identity_reason_unknown_input_stays_generic(raw):
    from server.web.trust_labels import identity_reason_ru

    assert identity_reason_ru(raw) == (
        "Сервер не получил подтверждённого описания ПК (инвентаризации). С токеном не связано."
    )


def test_untrusted_device_shows_the_reason_on_card_and_fleet(client):
    from server import db

    client.post("/api/v1/ingest", json=_env("dr", "inventory", {}, {"identity": _sh("timeout")}))
    risk = db.get_score_series("dr", limit=1)[0]["risk"]
    assert risk["identity_trust"] == {"state": "unavailable", "collector_status": "timeout"}

    card = client.get("/device/dr").text
    assert "Идентичность недостоверна" in card
    assert "не уложилась" in card
    assert "С токеном не связано" in card
    fleet = client.get("/").text
    assert "не уложилась" in fleet  # title of the untrusted badge


def _fleet_row(html: str, device_id: str) -> str:
    """HTML одной строки списка ПК: от её data-id до следующей строки."""
    start = html.index(f'data-id="{device_id}"')
    end = html.find('<tr class="devrow', start)
    return html[start:] if end == -1 else html[start:end]


def _health(state: str) -> dict:
    return {
        "state": state,
        "state_label": "здоров" if state == "h0" else "предотказ",
        "index": 90.0 if state == "h0" else 30.0,
        "band": "good" if state == "h0" else "bad",
        "confidence": "high",
        "dominant": None if state == "h0" else "storage",
        "dominant_label": None if state == "h0" else "накопитель",
        "delta_7d": None,
    }


@pytest.mark.parametrize(
    ("trust", "state", "shows_ok"),
    [("ok", "h0", True), ("ok", "h4", False), (None, "h0", False), ("untrusted", "h0", False)],
)
def test_fleet_ok_badge_only_for_a_trusted_calm_device(client, trust, state, shows_ok):
    """«OK» никогда не показывался доверенным ПК: шаблон проверял
    `not d.device_trust`, а там строка "ok". Теперь «OK» -- только доверенный
    ПК со спокойным вердиктом: зелёный «OK» рядом с «предотказом» был бы
    ложной уверенностью (ревью 2026-10-14). Нет данных доверия -- нет «OK»."""
    from datetime import datetime, timezone

    from server import db

    ts = datetime.now(timezone.utc).isoformat()
    risk: dict = {"domains": {}, "health": _health(state)}
    if trust:
        risk["device_trust"] = trust
    db.touch_device("dok", ts, "0.3.1", hostname="DOK")
    db.store_scores("dok", ts, {"risk": risk})

    row = _fleet_row(client.get("/").text, "dok")
    assert ('<span class="badge good">OK</span>' in row) is shows_ok


def test_fleet_page_still_renders(client):
    sh = {
        "storage_reliability": _sh("ok"),
        "reliability": _sh("ok"),
        "boot_time": _sh("ok"),
    }
    client.post("/api/v1/ingest", json=_env("df", "historical", healthy("historical"), sh))
    assert client.get("/").status_code == 200


def test_device_page_neutral_name_has_same_suffix_as_fleet(client):
    """Task 4b: безымянный ПК показывает один и тот же текст на /fleet и
    /device/{id} -- device_id-суффикс различает несколько «Без названия» в
    обоих местах одинаково (сейчас на карточке он голый, без суффикса)."""
    from server import db

    device_id = "a1b2c3d4e5"
    ts = "2026-05-30T00:00:00+00:00"
    risk = {"day1_factors": {"performance": [], "reliability": [], "wear": [], "risk_exposure": []}}
    db.touch_device(device_id, ts, "0.1.0")
    db.store_scores(device_id, ts, {"risk": risk})

    expected = f"Без названия ({device_id[-6:]})"
    assert expected in db.get_devices()[0]["display_name"]

    resp = client.get(f"/device/{device_id}")
    assert resp.status_code == 200
    assert expected in resp.text


def test_device_card_names_failed_collectors_in_russian(client):
    """Находка 10: «неизвест.» в покрытии не говорил, ПОЧЕМУ. Строка «Сбои сбора»
    называет источник и причину; законно пустые events и выведенный battery молчат."""
    import re

    from server import db

    sh = {"smart": _sh("timeout"), "reliability": _sh("ok"), "boot_time": _sh("ok")}
    client.post("/api/v1/ingest", json=_env("cf", "historical", healthy("historical"), sh))
    ev = _env("cf", "events", healthy("events"), {"events": _sh("empty")})
    client.post("/api/v1/ingest", json=ev)
    old = "2026-05-30T00:00:00+00:00"
    db.upsert_source_trust(
        "cf", "battery", "unavailable", 0.0, "timeout", "plausible", "", old, None
    )

    body = client.get("/device/cf").text
    line = body[body.index("Сбои сбора:") :]
    line = line[: line.index("<")]
    assert re.search(r"smart — не уложился во время \(\d+с назад\)", line), line
    assert "events" not in line
    assert "battery" not in line


def test_collector_failures_fail_open_on_unknown_status():
    """Незнакомый статус сборщика не прячется и печатается как есть (fail-open)."""
    from server.web.trust_labels import collector_failures

    rows = {"smart": {"collector_status": "weird", "ts": None}}
    assert collector_failures(rows) == [{"source": "smart", "reason": "weird", "age_sec": None}]


# --------------------------------------------------------------------------- #
# Агент 0.3.5: причина сбоя PowerShell доходит до доверия -- и не меняет его
# --------------------------------------------------------------------------- #
def _sh_reason(status: str, reason: str) -> dict:
    return {**_sh(status), "reason": reason}


def test_collector_reason_reaches_trust_row_blob_and_identity(client):
    from server import db

    sh = {"identity": _sh_reason("empty", "CommandNotFoundException")}
    client.post("/api/v1/ingest", json=_env("ps1", "inventory", {}, sh))
    row = db.get_source_trusts("ps1")["identity"]
    assert row["collector_reason"] == "CommandNotFoundException"
    risk = db.get_score_series("ps1", limit=1)[0]["risk"]
    assert risk["identity_trust"]["collector_reason"] == "CommandNotFoundException"


def test_reason_does_not_change_trust_state_weight_or_evidence(client):
    """Регресс-страж: один и тот же сбой с причиной и без -- одинаковое доверие."""
    from server import db

    client.post("/api/v1/ingest", json=_env("with", "inventory", {}, {"identity": _sh_reason("empty", "rc_1")}))
    client.post("/api/v1/ingest", json=_env("without", "inventory", {}, {"identity": _sh("empty")}))
    keys = ("state", "weight", "collector_status", "semantic_status", "reason")
    a = db.get_source_trusts("with")["identity"]
    b = db.get_source_trusts("without")["identity"]
    assert {k: a[k] for k in keys} == {k: b[k] for k in keys}
    assert (a["collector_reason"], b["collector_reason"]) == ("rc_1", None)


def test_repair_clears_the_reason(client):
    from server import db

    client.post("/api/v1/ingest", json=_env("fix", "inventory", {}, {"identity": _sh_reason("empty", "rc_1")}))
    client.post("/api/v1/ingest", json=_env("fix", "inventory", healthy("inventory"), {"identity": _sh("ok")}))
    assert db.get_source_trusts("fix")["identity"]["collector_reason"] is None


@pytest.mark.parametrize(
    ("token", "fragment"),
    [
        ("CommandNotFoundException", ".NET Framework Repair Tool"),
        ("CouldNotAutoloadMatchingModule", ".NET Framework Repair Tool"),
        ("cim_hollow", "winmgmt"),
        ("rc_1", "код 1"),
        ("InvokeMethodOnNull", "Сбой скрипта сбора: InvokeMethodOnNull"),
    ],
)
def test_ps_reason_hint_per_token(token, fragment):
    from server.web.trust_labels import identity_reason_ru, ps_reason_ru

    assert fragment in ps_reason_ru(token)
    text = identity_reason_ru({"state": "unavailable", "collector_status": "empty", "collector_reason": token})
    assert fragment in text and text.endswith("С токеном не связано.")


def test_engine_hint_names_the_real_task_restart_commands():
    from server.web.trust_labels import ps_reason_ru

    hint = ps_reason_ru("CommandNotFoundException")
    assert 'schtasks /end /tn "SRP Agent"' in hint and 'schtasks /run /tn "SRP Agent"' in hint


def test_collector_failures_shows_legit_empty_source_when_agent_names_a_cause():
    from server.web.trust_labels import collector_failures

    rows = {
        "events": {"collector_status": "empty", "ts": None, "collector_reason": "rc_1"},
        "certificates": {"collector_status": "empty", "ts": None},  # законно пусто
    }
    assert collector_failures(rows) == [
        {"source": "events", "reason": "пустой ответ (rc_1)", "age_sec": None}
    ]


def test_fleet_counts_live_pcs_with_broken_powershell(client):
    from server import db

    sh = {"identity": _sh_reason("empty", "CommandNotFoundException")}
    client.post("/api/v1/ingest", json=_env("psb", "inventory", {}, sh))
    html = client.get("/fleet/fragment").text
    assert 'data-filter="ps_broken"' in html and "PowerShell сломан" in html
    import re

    flags = re.search(r'data-flags="([^"]*)"[^>]*data-id="psb"', html)
    assert flags and "ps_broken" in flags.group(1).split()
    card = client.get("/device/psb").text
    assert ".NET Framework Repair Tool" in card  # баннер untrusted называет причину

    with db._connect() as conn:  # замолчал -> вердикт замёрз, в счётчик не идёт
        conn.execute("UPDATE devices SET last_seen=datetime('now','-1 day') WHERE device_id='psb'")
    after = client.get("/fleet/fragment").text
    # плитка остаётся (как все), иначе активный фильтр по ней опустошал бы таблицу
    tile = re.search(r'data-filter="ps_broken"[^>]*>\s*<div class="kn (\w+)">(\d+)<', after)
    assert tile and tile.groups() == ("na", "0")
