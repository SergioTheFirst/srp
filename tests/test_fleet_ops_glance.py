"""B2 (monitoring-informativity, 2026-08-27): fleet/deploy/netmap surfacing of
data already computed elsewhere -- no new collectors, no new thresholds.

2.1 -- /fleet KPI card «агенты устарели: N из M» (reuses per-device
``version_outdated`` from ``_enrich_fleet``, dashboard.py:338-374).
2.2 -- /deploy «Версии агентов в парке» (dashboard._version_counts() over the
same live/deduped devices as /fleet's summary -- review fix, see B2 below).
2.3 -- /netmap L3-edge tooltip merges ifindex into the existing «маршрут до
<cidr>» row (review fix: a second «сеть: <cidr>» row duplicated the cidr).

Review fixes (B2, this file, 2026-08-27): the L3-edge tooltip originally added
a SECOND row repeating the same cidr value already shown by the pre-existing
«маршрут до» row, wrapped in window.srpEsc despite row() being a textContent
sink; and /deploy's version distribution counted raw ``devices`` rows,
re-including a reinstalled machine's superseded (ghost) device_id that
/fleet's outdated KPI already excludes via ``_split_duplicates``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from server import db
from server.web import dashboard

pytestmark = pytest.mark.integration


# --------------------------------------------------------------------------- #
# 2.1 -- KPI: «агенты устарели: N из M»
# --------------------------------------------------------------------------- #
def test_fleet_summary_counts_outdated_agents(client: TestClient) -> None:
    """N (summary.outdated) is the existing version_outdated count; total unchanged."""
    db.upsert_device("v1", "2026-07-03T00:00:00+00:00", "0.3.0", hostname="NEW-PC")
    db.upsert_device("v2", "2026-07-03T00:00:00+00:00", "0.1.0", hostname="OLD-PC")
    db.upsert_device("v3", "2026-07-03T00:00:00+00:00", "0.1.0", hostname="OLD-PC-2")
    ctx = dashboard._fleet_context(db.get_devices())
    assert ctx["summary"]["outdated"] == 2
    assert ctx["summary"]["total"] == 3


def test_fleet_page_shows_outdated_agents_kpi_card(client: TestClient) -> None:
    db.upsert_device("v1", "2026-07-03T00:00:00+00:00", "0.3.0", hostname="NEW-PC")
    db.upsert_device("v2", "2026-07-03T00:00:00+00:00", "0.1.0", hostname="OLD-PC")
    html = client.get("/").text
    assert "агенты устарели" in html
    assert '<div class="kn bad">1 из 2</div>' in html
    assert "Версия агента ниже выложенной на сервере — устройства ждут обновления" in html


def test_fleet_page_outdated_kpi_neutral_when_none_outdated(client: TestClient) -> None:
    db.upsert_device("v1", "2026-07-03T00:00:00+00:00", "0.3.0", hostname="NEW-PC")
    html = client.get("/").text
    assert '<div class="kn na">0 из 1</div>' in html


# --------------------------------------------------------------------------- #
# 2.2 -- /deploy: «Версии агентов в парке»
# --------------------------------------------------------------------------- #
def test_version_counts_groups_and_orders() -> None:
    devices = [{"agent_version": "0.2.0"}, {"agent_version": "0.2.0"}, {"agent_version": "0.1.0"}]
    rows = dashboard._version_counts(devices)
    assert rows[0] == {"v": "0.2.0", "n": 2}
    assert rows[1] == {"v": "0.1.0", "n": 1}


def test_version_counts_missing_version_coalesces_to_dash() -> None:
    assert dashboard._version_counts([{"agent_version": None}]) == [{"v": "—", "n": 1}]


def test_deploy_outdated_version_row_has_explanatory_title(client: TestClient, monkeypatch) -> None:
    """LOW (review B1-B5 final): the warn-highlighted version row on /deploy
    had no title explaining WHY it's yellow, unlike the equivalent /fleet KPI
    (_fleet_body.html, "агенты устарели" -- same wording reused here).
    Unlike _enrich_fleet's version_outdated, _deploy_version_rows has no
    fallback to the fleet's own max version when no package is staged -- a
    staged "available" version must be supplied for any row to go .warn."""
    db.upsert_device("v1", "2026-07-03T00:00:00+00:00", "0.3.0", hostname="NEW-PC")
    db.upsert_device("v2", "2026-07-03T00:00:00+00:00", "0.1.0", hostname="OLD-PC")
    monkeypatch.setattr(dashboard, "_fleet_available_version", lambda request: "0.3.0")
    html = client.get("/deploy").text
    assert "Версия агента ниже выложенной на сервере — устройства ждут обновления" in html


def test_deploy_version_counts_exclude_reinstall_ghost(client: TestClient) -> None:
    """Review fix (server/db.py:3372 finding): counting raw ``devices`` rows
    re-includes a reinstalled machine's superseded device_id, which /fleet's
    outdated KPI already excludes via ``_split_duplicates`` -- the two pages
    must agree on «кого обновлять». The old, now-stale twin's version must not
    surface on /deploy once a fresher twin proves the reinstall."""
    from tests.conftest import envelope

    for did, ver in (("dev-old", "0.1.0"), ("dev-new", "0.3.0")):
        body = {**envelope(did, "liveness", {}), "agent_version": ver, "hostname": "PC-01"}
        assert client.post("/api/v1/ingest", json=body).status_code == 200
    with db._connect() as conn:
        conn.execute(
            "UPDATE devices SET last_seen=datetime('now','-2 hours') WHERE device_id='dev-old'"
        )
    live, _ = dashboard._live_devices(db.get_devices())
    assert dashboard._version_counts(live) == [{"v": "0.3.0", "n": 1}]
    html = client.get("/deploy").text
    assert "0.1.0" not in html


def test_deploy_version_rows_flags_below_staged_and_scales_bar() -> None:
    counts = [{"v": "0.3.0", "n": 6}, {"v": "0.1.0", "n": 2}]
    rows = dashboard._deploy_version_rows(counts, "0.3.0")
    assert rows[0]["outdated"] is False and rows[0]["pct"] == 100
    assert rows[1]["outdated"] is True and rows[1]["pct"] == 33  # round(2*100/6)


def test_deploy_version_rows_neutral_without_staged_package() -> None:
    """B2 2.2: no package staged -> nothing to compare against -> no highlighting."""
    counts = [{"v": "0.3.0", "n": 1}, {"v": "0.1.0", "n": 1}]
    rows = dashboard._deploy_version_rows(counts, None)
    assert all(r["outdated"] is False for r in rows)


def test_deploy_page_shows_version_distribution(client: TestClient) -> None:
    db.upsert_device("d1", "2026-07-03T00:00:00+00:00", "0.2.0", hostname="A")
    db.upsert_device("d2", "2026-07-03T00:00:00+00:00", "0.1.0", hostname="B")
    html = client.get("/deploy").text
    assert "Версии агентов в парке" in html
    assert "0.2.0" in html
    assert "0.1.0" in html


# --------------------------------------------------------------------------- #
# 2.3 -- /netmap: L3-edge tooltip gains «сеть: <cidr> · if <N>»
# --------------------------------------------------------------------------- #
def test_netmap_edge_tooltip_adds_ifindex_without_duplicating_cidr(client: TestClient) -> None:
    """Static JS-source check (no browser here) -- same idiom as the existing
    Sprint-3 tooltip-vocab tests in test_netmap_web.py.

    Review fix: ifindex must be merged into the existing «маршрут до <cidr>»
    row, not added as a second row repeating the same cidr value; and row()
    is a textContent sink ([[dashboard-xss-srpesc]]), so srpEsc must not wrap
    cidr/ifindex here -- that would double-escape and show literal HTML
    entities instead of the real value."""
    body = client.get("/netmap").text
    assert "маршрут до" in body  # existing row kept (test_netmap_web.py:215)
    assert "сеть: " not in body  # no longer a second row repeating the cidr
    assert body.count("if (L.cidr)") == 1  # one guarded block, not two
    assert "L.ifindex" in body  # ifindex still surfaced, merged into the row
    assert "window.srpEsc(L.cidr)" not in body
    assert "window.srpEsc(L.ifindex)" not in body
