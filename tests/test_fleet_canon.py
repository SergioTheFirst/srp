"""Канонический ПК: правило «запись вытеснена» (spec 2026-10-24-canonical-devices)."""

from __future__ import annotations

import pytest
from server.fleet_canon import ESTABLISHED_AFTER_SEC, dupe_key, superseded_by

pytestmark = pytest.mark.unit


def _row(did, host, *, first, last, stale=None, org=None, site=None, dept=None):
    """Возрасты в секундах (больше = раньше); stale по умолчанию -- старше 10 мин."""
    return {
        "device_id": did,
        "hostname": host,
        "first_seen_age_sec": first,
        "last_seen_age_sec": last,
        "stale": (last is not None and last > 600) if stale is None else stale,
        "org_code": org,
        "site_code": site,
        "dept_code": dept,
    }


def test_reinstall_ghost_superseded_by_live_successor():
    ghost = _row("old", "PC-01", first=900_000, last=7_200)
    new = _row("new", "PC-01", first=3_600, last=30)
    assert superseded_by([ghost, new]) == {"old": "new"}


def test_night_sleeping_successor_still_supersedes():
    """P0 владельца: преемник спит (stale), но прожил день -> дубль свёрнут и ночью."""
    ghost = _row("old", "PC-01", first=900_000, last=200_000)
    new = _row("new", "PC-01", first=150_000, last=40_000)  # жил ~30 ч, сейчас молчит
    assert superseded_by([ghost, new]) == {"old": "new"}


def test_single_forged_envelope_does_not_supersede():
    """Один конверт (прожил 0 с) и уже молчит -> не состоялся, жертва видна."""
    victim = _row("victim", "PC-01", first=900_000, last=86_400)
    forged = _row("forged", "PC-01", first=7_200, last=7_200)
    assert superseded_by([victim, forged]) == {}


def test_successor_established_by_lifetime_boundary():
    ghost = _row("old", "PC-01", first=900_000, last=200_000)
    almost = _row("k", "PC-01", first=100_000, last=100_000 - ESTABLISHED_AFTER_SEC + 1)
    assert superseded_by([ghost, almost]) == {}
    lived = _row("k", "PC-01", first=100_000, last=100_000 - ESTABLISHED_AFTER_SEC)
    assert superseded_by([ghost, lived]) == {"old": "k"}


def test_overlapping_lifetimes_are_namesakes_not_hidden():
    """Старая строка писала ПОСЛЕ появления новой -> перекрытие жизней: тёзки, обе видны
    (раньше stale-ветка прятала спящего реального тёзку)."""
    sleeping = _row("a", "PC-01", first=900_000, last=1_800)  # молчит 30 мин
    other = _row("b", "PC-01", first=3_600, last=20)  # появился час назад
    assert superseded_by([sleeping, other]) == {}


def test_both_live_nothing_superseded():
    a = _row("a", "PC-01", first=50_000, last=20)
    b = _row("b", "PC-01", first=40_000, last=40)
    assert superseded_by([a, b]) == {}


def test_chain_of_three_points_to_final_record():
    r1 = _row("r1", "PC-01", first=900_000, last=800_000)
    r2 = _row("r2", "PC-01", first=700_000, last=600_000)
    r3 = _row("r3", "PC-01", first=500_000, last=10)
    assert superseded_by([r3, r1, r2]) == {"r1": "r3", "r2": "r3"}


def test_newest_record_never_superseded_and_group_never_empties():
    rows = [
        # каждая прожила 5000 с (состоялась) и замолчала до появления следующей
        _row(f"r{i}", "PC-01", first=900_000 - i * 10_000, last=895_000 - i * 10_000)
        for i in range(4)
    ]
    ghosts = superseded_by(rows)
    assert "r3" not in ghosts  # самая поздняя -- канон
    assert set(ghosts) == {"r0", "r1", "r2"}


def test_cyrillic_hostname_casefold_groups():
    ghost = _row("old", " ПК-БУХ ", first=900_000, last=7_200)
    new = _row("new", "пк-бух", first=3_600, last=30)
    assert superseded_by([ghost, new]) == {"old": "new"}


def test_different_site_not_grouped():
    ghost = _row("a", "KASSA", first=900_000, last=7_200, site="msk")
    new = _row("b", "KASSA", first=3_600, last=30, site="spb")
    assert superseded_by([ghost, new]) == {}


def test_empty_hostname_and_unparseable_ages_out_of_comparison():
    rows = [
        _row("a", "", first=900_000, last=7_200),
        _row("b", "", first=3_600, last=30),
        _row("c", "PC-01", first=None, last=7_200),
        _row("d", "PC-01", first=3_600, last=None),
        _row("e", "PC-01", first=900_000, last=7_200),
    ]
    assert superseded_by(rows) == {}


def test_dupe_key_strips_and_casefolds_all_parts():
    assert dupe_key(
        {"org_code": " O1 ", "site_code": "MSK", "dept_code": None, "hostname": "Pc"}
    ) == (
        "o1",
        "msk",
        "",
        "pc",
    )


# --------------------------------------------------------------------------- #
# db.superseded_map: правило на реальной БД (серверные метки)
# --------------------------------------------------------------------------- #
def _ingest(client, did, host):
    from tests.conftest import envelope

    body = {**envelope(did, "liveness", {}), "hostname": host}
    assert client.post("/api/v1/ingest", json=body).status_code == 200


@pytest.mark.integration
def test_db_superseded_map_on_real_rows(client):
    from server import db

    _ingest(client, "dev-old", "PC-01")
    _ingest(client, "dev-new", "PC-01")
    _ingest(client, "dev-other", "PC-02")
    with db._connect() as conn:
        conn.execute(
            "UPDATE devices SET first_seen=datetime('now','-30 days'),"
            " last_seen=datetime('now','-2 hours') WHERE device_id='dev-old'"
        )
    assert db.superseded_map() == {"dev-old": "dev-new"}
    assert db.superseded_ids() == frozenset({"dev-old"})


@pytest.mark.integration
def test_device_card_shows_lineage_both_ways(client):
    from server import db

    _ingest(client, "dev-old", "PC-01")
    _ingest(client, "dev-new", "PC-01")
    with db._connect() as conn:
        conn.execute(
            "UPDATE devices SET first_seen=datetime('now','-30 days'),"
            " last_seen=datetime('now','-2 hours') WHERE device_id='dev-old'"
        )
    ghost_html = client.get("/device/dev-old").text
    assert "Запись вытеснена переустановкой" in ghost_html
    assert 'href="/device/dev-new"' in ghost_html
    canon_html = client.get("/device/dev-new").text
    assert "Старые записи этого ПК" in canon_html
    assert 'href="/device/dev-old"' in canon_html
    assert "Запись вытеснена" not in canon_html


@pytest.mark.integration
def test_api_devices_canonical_by_default_all_on_request(client):
    from server import db

    _ingest(client, "dev-old", "PC-01")
    _ingest(client, "dev-new", "PC-01")
    with db._connect() as conn:
        conn.execute(
            "UPDATE devices SET first_seen=datetime('now','-30 days'),"
            " last_seen=datetime('now','-2 hours') WHERE device_id='dev-old'"
        )
    canon = {d["device_id"] for d in client.get("/api/v1/devices").json()}
    assert canon == {"dev-new"}
    everything = {d["device_id"] for d in client.get("/api/v1/devices?all=1").json()}
    assert everything == {"dev-old", "dev-new"}


@pytest.mark.integration
def test_zabbix_verdicts_skip_ghost_so_live_successor_is_exported(client):
    """Раньше призрак + живой преемник с одним hostname оба уходили в duplicate_name."""
    from datetime import datetime, timezone

    from server import db
    from server.zabbix.config import load_zabbix_config
    from server.zabbix.items import build_packet

    now = datetime.now(timezone.utc).isoformat()
    risk = {"health": {"state": "h0", "damage": {"value": 5.0}, "dominant_label": "—"}}
    for did in ("dev-old", "dev-new"):
        db.touch_device(did, now, "0.3.4", hostname="PC-01")
        db.store_scores(did, now, {"risk": risk})
    with db._connect() as conn:
        conn.execute(
            "UPDATE devices SET first_seen=datetime('now','-30 days'),"
            " last_seen=datetime('now','-2 hours') WHERE device_id='dev-old'"
        )
    rows = db.get_fleet_verdicts()
    assert [r["device_id"] for r in rows] == ["dev-new"]
    packet = build_packet(rows, load_zabbix_config({}))
    assert packet.skipped == []
    assert [device_id for _host, device_id in packet.hosts] == ["dev-new"]


def test_large_same_name_group_is_not_quadratic():
    """Ревью 2026-10-24 (HIGH): тысячи поддельных id с одним именем не должны вешать
    каждый опрос флота. 20k строк в одной группе -- быстрее секунды."""
    import time

    rows = [_row(f"f{i}", "PC-01", first=i + 10, last=i + 10) for i in range(20_000)]
    rows.append(_row("live", "PC-01", first=5, last=1))
    t0 = time.perf_counter()
    ghosts = superseded_by(rows)
    assert time.perf_counter() - t0 < 1.0
    assert "live" not in ghosts
    assert all(canon == "live" for canon in ghosts.values())


def test_cycle_on_corrupt_timestamps_never_hides_both():
    """first_seen позже last_seen (кривые данные) -- не должно спрятать всю группу."""
    a = _row("a", "PC-01", first=10, last=500, stale=False)
    b = _row("b", "PC-01", first=20, last=400, stale=False)
    ghosts = superseded_by([a, b])
    assert len(ghosts) < 2
    assert all(g != c for g, c in ghosts.items())
