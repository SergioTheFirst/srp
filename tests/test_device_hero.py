"""ssd3 Ф7 T7.2 -- tests for the device-detail "hero" block (health coordinates
summary above "Прогноз — траектории и ресурс" on server/web/templates/device.html).

Two layers, matching tests/test_health_web.py's own split:

* A pure data-assembly helper in ``server.web.dashboard`` (index-series
  extraction for the sparkline) -- unit-tested directly, no DB.
* The ``/device/{id}`` route + template -- integration-tested through the
  ``client`` fixture (seeded straight via db.touch_device/store_scores, same
  pattern as test_health_web.py's own ``_seed``, so each fixture can pin an
  exact ``health`` shape without going through the full scoring pipeline).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from server import db
from server.web import dashboard

pytestmark = pytest.mark.integration

_HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# --------------------------------------------------------------------------- #
# _health_index_series (pure; the score-series extraction dashboard.py's
# device route feeds to the hero sparkline's |tojson island)
# --------------------------------------------------------------------------- #
def test_health_index_series_skips_rows_with_no_health_key() -> None:
    rows = [  # newest-first, as get_score_series returns
        {"ts": "2026-07-10T00:00:00+00:00", "risk": {"health": {"index": 60.0}}},
        {"ts": "2026-07-09T00:00:00+00:00", "risk": {}},  # pre-Ф6 row: no health key
        {"ts": "2026-07-08T00:00:00+00:00", "risk": {"health": {"index": 80.0}}},
    ]
    out = dashboard._health_index_series(rows)
    assert out == [
        {"ts": "2026-07-10T00:00:00+00:00", "index": 60.0},
        {"ts": "2026-07-08T00:00:00+00:00", "index": 80.0},
    ]


def test_health_index_series_skips_none_index() -> None:
    rows = [{"ts": "t1", "risk": {"health": {"index": None}}}]
    assert dashboard._health_index_series(rows) == []


def test_health_index_series_empty_input_is_empty_output() -> None:
    assert dashboard._health_index_series([]) == []


def test_health_index_series_preserves_newest_first_order() -> None:
    rows = [
        {"ts": "t3", "risk": {"health": {"index": 30.0}}},
        {"ts": "t2", "risk": {"health": {"index": 20.0}}},
        {"ts": "t1", "risk": {"health": {"index": 10.0}}},
    ]
    out = dashboard._health_index_series(rows)
    assert [r["ts"] for r in out] == ["t3", "t2", "t1"]  # order preserved, JS reverses


# --------------------------------------------------------------------------- #
# Route + template integration
# --------------------------------------------------------------------------- #
def _seed(device_id: str, hostname: str, risk: dict, ts: str = "") -> None:
    """Same minimal-seed pattern as test_health_web.py's own ``_seed`` (writes
    straight to storage, skipping the scoring pipeline). device.html's own
    (pre-existing, untouched) "Диагностика" drilldown unconditionally chains
    ``s.risk.day1_factors.performance`` with no ``or {}`` guard -- unlike every
    other ``s.risk.*`` access on that page -- so a minimal risk blob needs this
    default or the page 500s before the hero block even gets a chance to render.
    """
    ts = ts or _iso(_now())
    risk = {
        "day1_factors": {"performance": [], "reliability": [], "wear": [], "risk_exposure": []},
        **risk,
    }
    db.touch_device(device_id, ts, "0.1.0", hostname=hostname)
    db.store_scores(device_id, ts, {"risk": risk})


def _real_health(**over: Any) -> dict:
    base = {
        "damage": {"value": 20.0, "band": "watch", "confidence": "high", "evidence": []},
        "resilience": {"value": 85.0, "band": "good", "confidence": "high", "evidence": []},
        "observability": {"value": 90.0, "band": "good", "confidence": "high", "evidence": []},
        "blind_spots": [],
        "state": "h1",
        "state_label": "ранняя деградация",
        "state_evidence": [],
        "index": 78.0,
        "band": "watch",
        "confidence": "high",
        "dominant": "storage",
        "dominant_label": "накопитель",
        "horizon_days": None,
        "horizon_reason": "",
        "action": "снять образ данных, планировать замену накопителя",
        "factors": [],
        "missing_evidence": [],
        "delta_7d": -3.5,
    }
    base.update(over)
    return base


def _blind_health(**over: Any) -> dict:
    base = {
        "damage": {"value": None, "band": "unknown", "confidence": "unknown", "evidence": []},
        "resilience": {"value": None, "band": "unknown", "confidence": "unknown", "evidence": []},
        "observability": {
            "value": 20.0,
            "band": "bad",
            "confidence": "low",
            "evidence": [],
        },
        "blind_spots": ["SMART недоступен", "нет анализа событий"],
        "state": "unknown",
        "state_label": "нет видимости",
        "state_evidence": [],
        "index": None,
        "band": "unknown",
        "confidence": "unknown",
        "dominant": None,
        "dominant_label": "не определено",
        "horizon_days": None,
        "horizon_reason": "",
        "action": "восстановить видимость: проверить агент и доступ к SMART/журналам",
        "factors": [],
        "missing_evidence": [],
        "delta_7d": None,
    }
    base.update(over)
    return base


def _hero_fragment(body: str) -> str:
    """The hero block's own HTML, isolated from the rest of the page (base.html's
    :root token block legitimately contains hex literals, so a whole-page scan is
    not a valid pin -- match test_health_web.py's own island-isolation approach).

    Since the risk-hierarchy reorder (ssd3/cctodo W4.3-3), the hero renders
    immediately before the "Оси score100" label, not before "Прогноз" -- end the
    slice there so it stays a tight hero-only window instead of also swallowing
    the score100 axis cards and source-coverage widget that now sit between the
    hero and "Прогноз"."""
    start = body.index('id="device-hero"')
    end = body.index("Оси score100")
    return body[start:end]


def test_hero_renders_three_coordinate_bars_labelled_in_russian(client) -> None:
    _seed("hero-1", "HERO-1", {"health": _real_health()})
    body = client.get("/device/hero-1").text
    assert client.get("/device/hero-1").status_code == 200
    frag = _hero_fragment(body)
    assert "Повреждения" in frag
    assert "Устойчивость" in frag
    assert "Наблюдаемость" in frag


def test_hero_index_caption_present(client) -> None:
    _seed("hero-2", "HERO-2", {"health": _real_health(index=78.0)})
    body = client.get("/device/hero-2").text
    frag = _hero_fragment(body)
    assert "проекция (D, R, O)" in frag
    assert "78" in frag


def test_hero_ladder_highlights_exactly_current_state(client) -> None:
    _seed("hero-3", "HERO-3", {"health": _real_health(state="h3", band="bad")})
    body = client.get("/device/hero-3").text
    frag = _hero_fragment(body)
    assert frag.count("ladder-step current") == 1
    # the current step must be the h3 one, not some other rung
    assert "ускоренная деградация" in frag


def test_hero_unknown_state_shows_insufficient_data_not_healthy(client) -> None:
    _seed("hero-4", "HERO-4", {"health": _blind_health()})
    body = client.get("/device/hero-4").text
    frag = _hero_fragment(body)
    assert "данных недостаточно" in frag
    assert "ladder-step current" not in frag  # no ladder step ever highlighted
    assert "здоров" not in frag  # h0's own label never appears -- never dressed up as healthy
    assert "SMART недоступен" in frag  # blind_spots surfaced


def test_hero_unknown_state_never_shows_coordinate_bars(client) -> None:
    _seed("hero-5", "HERO-5", {"health": _blind_health()})
    body = client.get("/device/hero-5").text
    frag = _hero_fragment(body)
    # the coordinate-bar labels must not appear in the blind branch
    assert "Повреждения" not in frag
    assert "Устойчивость" not in frag


def test_hero_missing_health_shows_muted_note_not_crash(client) -> None:
    _seed("hero-6", "HERO-6", {})  # no "health" key at all (pre-Ф6 device)
    r = client.get("/device/hero-6")
    assert r.status_code == 200
    frag = _hero_fragment(r.text)
    assert "нет данных о координатах здоровья" in frag


def test_hero_staleness_banner_appears_for_old_score_and_not_fresh(client) -> None:
    old_ts = _iso(_now() - timedelta(days=5))
    _seed("hero-7", "HERO-7", {"health": _real_health()}, ts=old_ts)
    stale_body = client.get("/device/hero-7").text
    stale_frag = _hero_fragment(stale_body)
    assert "данные устарели" in stale_frag

    fresh_ts = _iso(_now())
    _seed("hero-8", "HERO-8", {"health": _real_health()}, ts=fresh_ts)
    fresh_body = client.get("/device/hero-8").text
    fresh_frag = _hero_fragment(fresh_body)
    assert "данные устарели" not in fresh_frag


def _strip_scripts(frag: str) -> str:
    """Drop <script>...</script> bodies before the hex-colour scan. The Plotly
    sparkline's JS carries a defensive ``getPropertyValue("--accent").trim() ||
    "#0ea5e9"`` fallback -- the SAME idiom this page's own pre-existing
    chart-disk-p95 code and health.html's charts already use (colour still
    SOURCED from the CSS token; the hex is only a last-resort fallback if the
    lookup itself fails). That is a distinct, already-reviewed pattern from
    "a band colour hardcoded instead of driven by band_class" -- the thing this
    structural pin actually guards against in the static HTML/CSS markup."""
    return re.sub(r"<script[^>]*>.*?</script>", "", frag, flags=re.S)


def test_hero_no_hardcoded_hex_colors_in_markup(client) -> None:
    _seed("hero-9", "HERO-9", {"health": _real_health()})
    body = client.get("/device/hero-9").text
    frag = _strip_scripts(_hero_fragment(body))
    assert not _HEX.search(frag), f"hardcoded hex colour found in hero markup: {_HEX.search(frag)}"


def test_hero_no_hardcoded_hex_colors_in_blind_branch(client) -> None:
    _seed("hero-10", "HERO-10", {"health": _blind_health()})
    body = client.get("/device/hero-10").text
    frag = _strip_scripts(_hero_fragment(body))
    assert not _HEX.search(frag), (
        f"hardcoded hex colour found in blind hero markup: {_HEX.search(frag)}"
    )


def test_hero_dominant_and_action_line_present(client) -> None:
    _seed(
        "hero-11",
        "HERO-11",
        {"health": _real_health(dominant="battery", dominant_label="батарея")},
    )
    body = client.get("/device/hero-11").text
    frag = _hero_fragment(body)
    assert "батарея" in frag  # dominant_label
    assert "заменить батарею" in frag  # action_for("battery")


def test_hero_delta_7d_none_renders_neutral_dash_not_broken(client) -> None:
    _seed("hero-12", "HERO-12", {"health": _real_health(delta_7d=None)})
    r = client.get("/device/hero-12")
    assert r.status_code == 200
    frag = _hero_fragment(r.text)
    assert "Δ7д" in frag


def test_hero_sparkline_island_embeds_index_series(client) -> None:
    _seed("hero-13", "HERO-13", {"health": _real_health()})
    body = client.get("/device/hero-13").text
    frag = _hero_fragment(body)
    assert 'id="hero-health-series"' in frag
    assert "78.0" in frag


def test_device_page_hero_precedes_score100_axes(seeded_client) -> None:
    devices = seeded_client.get("/api/v1/devices").json()
    assert devices
    html = seeded_client.get(f"/device/{devices[0]['device_id']}").text
    hero = html.find('id="device-hero"')
    axes = html.find("Оси score100")
    assert hero != -1 and axes != -1, "нет hero или подписи осей"
    assert hero < axes, "вердикт D/R/O должен идти раньше score100-детализации"
