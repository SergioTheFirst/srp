"""Ф4: unified network-map page + canvas engine (netmap-unification).

``/netmap`` now serves the ONE unified superset graph (Ф2 assembler via the Ф3
GraphCache) -- nodes/links/subnets/totals -- through the ``_netgraph.html`` canvas
engine. The old ephemeral cluster model (``build_netmap`` clusters) is retired.
"""

from __future__ import annotations

import json
import re

import pytest
from tests.conftest import healthy

pytestmark = pytest.mark.integration


def _net_payload(ip="192.168.1.10", mac="AA-BB-CC-00-00-01", gw="192.168.1.1", loss=0.0):
    p = healthy("historical")
    p["network_adapters"] = [
        {
            "name": "Ethernet",
            "kind": "ethernet",
            "mac": mac,
            "up": True,
            "ipv4": [ip],
            "gateway": gw,
        }
    ]
    p["network_neighbors"] = [
        {"ip": "192.168.1.50", "mac": "00-50-56-00-00-09", "state": "Reachable"}
    ]
    p["network_quality"] = [
        {"target_kind": "gateway", "target": gw, "latency_ms": 1.5, "loss_pct": loss, "samples": 3}
    ]
    return p


def _ingest(client, did, payload):
    env = {
        "device_id": did,
        "agent_version": "0.1.0",
        "msg_type": "historical",
        "payload": payload,
        "source_health": {"network": {"status": "ok", "collected_at": "2026-06-10T00:00:00+00:00"}},
    }
    r = client.post("/api/v1/ingest", json=env)
    assert r.status_code == 200, r.text


def _embedded_graph(body: str) -> dict:
    m = re.search(r'<script id="netgraph-data" type="application/json">(.*?)</script>', body, re.S)
    assert m, "embedded netgraph JSON island missing"
    return json.loads(m.group(1))


# --------------------------------------------------------------------------- #
# API + page contract (Ф3 unified graph served to Ф4 canvas)
# --------------------------------------------------------------------------- #
def test_netmap_api_and_page(client):
    _ingest(client, "map-11", _net_payload(loss=30.0))
    _ingest(client, "map-12", _net_payload(ip="192.168.1.11", mac="AA-BB-CC-00-00-02", loss=40.0))
    api = client.get("/api/v1/netmap")
    assert api.status_code == 200
    m = api.json()
    # Ф3: /api/v1/netmap is a deprecated alias of the unified graph.
    assert m["totals"]["agents"] == 2
    assert any(s["anomaly"] for s in m["subnets"])

    page = client.get("/netmap")
    assert page.status_code == 200
    body = page.text
    assert "Карта сети" in body and "map-11" in body


def test_device_page_shows_axis_and_subnet_note(client):
    _ingest(client, "map-21", _net_payload(loss=30.0))
    _ingest(client, "map-22", _net_payload(ip="192.168.1.11", mac="AA-BB-CC-00-00-02", loss=40.0))
    page = client.get("/device/map-21")
    assert page.status_code == 200
    body = page.text
    assert "Здоровье сети" in body  # axis card
    assert "инфраструктур" in body  # subnet annotation (D8)
    assert "Качество связи" in body  # probes table


def test_diagnostics_exposes_network_risk(client):
    _ingest(client, "map-31", _net_payload())
    d = client.get("/api/v1/diagnostics/map-31")
    assert d.status_code == 200
    assert d.json()["network_risk"]["value"] is not None


# --------------------------------------------------------------------------- #
# Ф4: unified canvas engine (one graph, all types, wireless, quality, XSS)
# --------------------------------------------------------------------------- #
def test_netmap_page_embeds_canvas_and_unified_graph(client):
    _ingest(client, "map-41", _net_payload())
    body = client.get("/netmap").text
    assert 'id="netgraph-canvas"' in body
    g = _embedded_graph(body)
    # unified shape, not the retired cluster model
    assert set(g) == {"nodes", "links", "subnets", "totals"}
    assert g["totals"]["nodes"] >= 2  # agent + gateway
    types = {n["dev_type"] for n in g["nodes"]}
    assert "agent" in types and "router" in types
    # device_id survives the round-trip -> canvas click-through builds /device/{id}
    agent = next(n for n in g["nodes"] if n["device_id"] == "map-41")
    assert agent["card_url"] == "/device/map-41"


def test_netmap_inventory_table_renders_without_js(client):
    """SSR inventory table is available without JS: one row per graph node, each
    linking to its canonical card (card_url)."""
    _ingest(client, "map-42", _net_payload())
    body = client.get("/netmap").text
    assert 'class="net-inv"' in body  # the SSR inventory table
    assert "Инвентарь сети" in body
    # the agent row links to its canonical card, not a generic netdisco route
    assert 'href="/device/map-42"' in body


def test_netmap_has_poll_button_and_changelog(client):
    """Ф4 keeps the 'собрать сейчас' button + changelog from the topology page."""
    _ingest(client, "map-43", _net_payload())
    body = client.get("/netmap").text
    assert "Собрать карту сейчас" in body
    assert 'id="net-poll-btn"' in body
    assert "Журнал изменений" in body


def test_netmap_anomaly_overlay_lands_on_gateway(client):
    """The subnet anomaly overlay is computed by the Ф2 assembler and surfaced to the
    canvas: the degraded subnet is flagged and the gateway carries the overlay."""
    _ingest(client, "map-51", _net_payload(loss=30.0))
    _ingest(client, "map-52", _net_payload(ip="192.168.1.11", mac="AA-BB-CC-00-00-02", loss=40.0))
    body = client.get("/netmap").text
    g = _embedded_graph(body)
    anom = [s for s in g["subnets"] if s["anomaly"]]
    assert anom, "degraded subnet must be flagged"
    # the gateway node exists and sits in that subnet
    gw = next(n for n in g["nodes"] if n["dev_type"] == "router")
    assert gw["subnet"] == anom[0]["subnet_hint"]
    assert g["totals"]["anomalies"] >= 1


def test_netmap_canvas_enrichment_layers_and_fields(client):
    """Sprint-1 enrichment: the page exposes the freshness/risk layers + the hide-stale
    toggle, and the embedded graph carries the new per-node/per-link fields the canvas
    renders (freshness age, chokepoints, edge physics) -- pinned as strings + on-page
    data because pytest cannot execute the canvas JS."""
    _ingest(client, "map-91", _net_payload())
    body = client.get("/netmap").text
    # new control-panel affordances (S1 freshness, S4 risk, hide-stale toggle)
    assert "свежесть (возраст)" in body
    assert "риск (точки отказа)" in body
    assert "скрыть устаревшие" in body
    # tooltip vocabulary present in the engine source (S4 / S3)
    assert "точка единого отказа" in body
    assert "порт down на живом устройстве" in body
    # the data the canvas needs actually reaches the page
    g = _embedded_graph(body)
    assert all("first_seen" in n and "last_seen" in n and "articulation" in n for n in g["nodes"])
    assert all("speed_mbps" in e and "port_down" in e and "bridge" in e for e in g["links"])
    # the lone agent->gateway uplink is a bridge: removing it strands the agent
    assert any(e["bridge"] for e in g["links"])


def test_netmap_wireless_uplink_marked(client):
    """A Wi-Fi agent uplink is tagged medium=wireless (Ф2 heuristic) and reaches the
    canvas engine so it renders dashed."""
    p = _net_payload()
    p["network_adapters"][0]["kind"] = "wifi"
    _ingest(client, "map-61", p)
    body = client.get("/netmap").text
    g = _embedded_graph(body)
    up = next(edge for edge in g["links"] if edge["link_kind"] == "agent-uplink")
    assert up["medium"] == "wireless"


def test_netmap_hides_arp_only_nodes(client):
    # The agentless ARP neighbour from _net_payload (192.168.1.50) must not appear on
    # the map -- not in the SSR body, not in the canvas JSON island. Gateways and
    # agents still show (owner 2026-06-22; Ф4 carries the invariant forward).
    _ingest(client, "map-71", _net_payload())
    body = client.get("/netmap").text
    assert "192.168.1.50" not in body  # ARP-only IP gone from SSR + JSON island
    assert "без агента" not in body  # the ARP-only stat/legend label is gone
    g = _embedded_graph(body)
    assert not any(n.get("ip") == "192.168.1.50" for n in g["nodes"])
    assert g["nodes"]  # gateway + agent still present


def test_netmap_page_without_data_has_no_canvas(client):
    body = client.get("/netmap").text
    assert 'id="netgraph-canvas"' not in body
    assert "карта появится" in body  # empty state survives


def test_netmap_embedded_json_cannot_break_out_of_script(client):
    """A hostile hostname must not terminate the JSON <script> island (XSS pin)."""
    inv = healthy("inventory")
    inv["hostname"] = "</script><script>alert(1)//"
    r = client.post(
        "/api/v1/ingest",
        json={
            "device_id": "map-81",
            "agent_version": "0.1.0",
            "msg_type": "inventory",
            "payload": inv,
        },
    )
    assert r.status_code == 200, r.text
    _ingest(client, "map-81", _net_payload())
    body = client.get("/netmap").text
    assert "</script><script>alert(1)" not in body
    g = _embedded_graph(body)
    # the value itself survives intact after JSON parsing
    agent = next(n for n in g["nodes"] if n["device_id"] == "map-81")
    assert agent["hostname"] == "</script><script>alert(1)//"


def test_netmap_page_inert_against_event_handler_payload(client):
    """An event-handler payload (img onerror / svg onload / javascript: href) must
    reach the page inert: autoescape neutralises the SSR table, the canvas engine
    never injects innerHTML, and click-through uses only the assembler card_url --
    so the raw payload string cannot execute. Hardens the XSS-pin against regressions
    beyond the </script> JSON-island breakout."""
    inv = healthy("inventory")
    inv["hostname"] = "<img src=x onerror=alert(1)><svg onload=alert(2)>"
    r = client.post(
        "/api/v1/ingest",
        json={
            "device_id": "map-82",
            "agent_version": "0.1.0",
            "msg_type": "inventory",
            "payload": inv,
        },
    )
    assert r.status_code == 200, r.text
    _ingest(client, "map-82", _net_payload())
    body = client.get("/netmap").text
    # No live, executable HTML tag reaches the DOM: the SSR table escapes ``<`` to
    # ``&lt;`` (so the tag is inert text), and the canvas engine never injects
    # innerHTML. The inert ``onerror=alert`` substring survives only inside the JSON
    # data island (where it is a string value, not parsed as HTML) -- so we assert
    # the *executable* vector (an un-escaped tag) is absent, not the bare substring.
    assert "<img" not in body  # no un-escaped img tag anywhere
    assert "<svg onload" not in body
    assert "<script>alert" not in body
    assert "javascript:" not in body
    # the escaped form IS present (autoescape turned < into &lt;) -- proves it passed
    # through the SSR table safely rather than being stripped silently
    assert "&lt;img" in body


def test_netmap_renders_all_glyph_types(client):
    """Every glyph type the engine draws must render: router/switch/ap/agent/printer/
    server/phone/endpoint. We feed the assembler a mixed fleet so all dev_types show
    in the unified graph."""
    from server import db

    # net_devices carry every dev_type the engine must glyph.
    devices = [
        {"device_nid": "nd-router-1", "dev_type": "router", "ip": "10.0.0.1", "status": "up"},
        {"device_nid": "nd-switch-1", "dev_type": "switch", "ip": "10.0.0.2", "status": "up"},
        {"device_nid": "nd-ap-1", "dev_type": "ap", "ip": "10.0.0.3", "status": "up"},
        {"device_nid": "nd-srv-1", "dev_type": "server", "ip": "10.0.0.4", "status": "up"},
        {"device_nid": "nd-ph-1", "dev_type": "phone", "ip": "10.0.0.5", "status": "up"},
        {"device_nid": "nd-ep-1", "dev_type": "endpoint", "ip": "10.0.0.6", "status": "up"},
        {"device_nid": "nd-prn-1", "dev_type": "unknown", "ip": "10.0.0.7", "status": "up"},
    ]
    for d in devices:
        db.upsert_net_device(d)
    # a linked printer node (printer_id -> printer glyph) and an agent node. The
    # identity FK is bound by set_net_device_links (as the inventory cycle does).
    db.set_net_device_links("nd-prn-1", printer_id="prn-1")
    _ingest(client, "map-91", _net_payload(ip="10.0.0.9"))

    # invalidate the cache so the page rebuilds over the just-stored backbone
    cache = getattr(client.app.state, "network_map_cache", None)
    if cache is not None:
        cache.invalidate()

    body = client.get("/netmap").text
    g = _embedded_graph(body)
    types = {n["dev_type"] for n in g["nodes"]}
    for t in ("router", "switch", "ap", "agent", "printer", "server", "phone", "endpoint"):
        assert t in types, f"missing glyph type on the map: {t}"


# --------------------------------------------------------------------------- #
# Ф5: control panel (filters / layers / layout / side panel / export / time)
# --------------------------------------------------------------------------- #
def test_netmap_page_renders_control_panel(client):
    """Ф5 panel scaffold is present: the panel container, filter sections, layers,
    layout select, side panel, and export/save buttons."""
    _ingest(client, "map-p1", _net_payload())
    body = client.get("/netmap").text
    assert 'id="ng-panel"' in body
    # filter sections
    assert 'id="ng-f-types"' in body and 'id="ng-f-status"' in body
    assert 'id="ng-f-medium"' in body and 'id="ng-f-conf"' in body
    assert 'id="ng-f-prov"' in body and 'id="ng-f-subnet"' in body
    # presets + layers + layout + nav + view
    assert 'id="ng-presets"' in body and 'id="ng-layers"' in body
    assert 'id="ng-layout"' in body and 'id="ng-side"' in body
    assert 'id="ng-save-view"' in body
    assert (
        'id="ng-export-png"' in body
        and 'id="ng-export-csv"' in body
        and 'id="ng-export-json"' in body
    )
    # ADV navigation
    assert 'id="ng-isolate"' in body and 'id="ng-path"' in body and 'id="ng-cause"' in body


def test_netmap_presets_include_hide_unconfirmed(client):
    """The 'hide unconfirmed' preset reinterprets 'hide ARP-only' for the unified
    graph (ARP-only nodes are already excluded; net-only identity remains)."""
    _ingest(client, "map-p2", _net_payload())
    body = client.get("/netmap").text
    assert "скрыть неподтверждённые" in body
    assert "только инфра" in body
    assert "сбросить фильтры" in body


def test_netmap_time_machine_mount_and_snapshots_island(client):
    """The snapshots list is rendered to the SSR page + a JSON island drives the
    time-machine select. With no snapshots present the mount is absent (graceful)."""
    from server import db

    db.store_topology_snapshot({"nodes": [], "links": []}, received_at="2026-06-20T00:00:00+00:00")
    _ingest(client, "map-p3", _net_payload())
    body = client.get("/netmap").text
    assert 'id="ng-timemachine"' in body  # mount present only when snapshots exist
    assert 'id="netmap-snapshots"' in body  # JSON island
    assert '"received_at"' in body  # a snapshot row serialized


def test_netmap_time_machine_historical_frame_renders(client):
    """?at=<id> renders the historical frame with the unified graph + a plaque and
    the no-overlays note (live overlays are not computed for a past frame)."""
    from server import db

    graph = {
        "nodes": [{"nid": "nd-r1", "dev_type": "router", "ip": "10.0.0.1"}],
        "links": [],
    }
    db.store_topology_snapshot(graph, received_at="2026-06-20T00:00:00+00:00")
    sid = db.list_topology_snapshots(limit=1)[0]["id"]
    body = client.get(f"/netmap?at={sid}").text
    assert 'id="netgraph-canvas"' in body
    g = _embedded_graph(body)
    assert g["totals"]["nodes"] == 1
    # the page marks the frame historical (SSR note from the `history` context)
    assert "исторический кадр" in body
    assert "Live-оверлеи" in body  # the no-overlays note for a past frame


def test_netmap_page_no_innerhtml_in_engine(client):
    """Ф5 hardens the XSS boundary: the new sinks (side panel, edge tooltip, export,
    history plaque) reach the DOM only via textContent -- no innerHTML /
    insertAdjacentHTML / outerHTML in the engine script."""
    _ingest(client, "map-p4", _net_payload())
    body = client.get("/netmap").text
    # the engine island is a single <script>...</script>; extract it loosely
    assert ".innerHTML" not in body
    assert "insertAdjacentHTML" not in body
    assert "outerHTML" not in body


def test_netmap_panel_inert_against_event_handler_payload(client):
    """Hardens Ф4's XSS-pin across the new Ф5 sinks: an event-handler payload
    (img onerror / svg onload / javascript:) reaches the page inert. The side panel,
    edge tooltip and history plaque build DOM via textContent, the SSR table escapes
    ``<``, and the canvas never injects HTML -- so the raw payload cannot execute."""
    inv = healthy("inventory")
    inv["hostname"] = "<img src=x onerror=alert(1)><svg onload=alert(2)>"
    r = client.post(
        "/api/v1/ingest",
        json={
            "device_id": "map-p5",
            "agent_version": "0.1.0",
            "msg_type": "inventory",
            "payload": inv,
        },
    )
    assert r.status_code == 200, r.text
    _ingest(client, "map-p5", _net_payload())
    body = client.get("/netmap").text
    # no un-escaped executable tag reaches the DOM anywhere (panel + SSR + island)
    assert "<img" not in body
    assert "<svg onload" not in body
    assert "<script>alert" not in body
    assert "javascript:" not in body
    # autoescape turned ``<`` into ``&lt;`` (passed through safely, not stripped)
    assert "&lt;img" in body


def test_netmap_path_engine_terminates(client):
    """Regression pin for the Ф5 pathBfs infinite-loop (code-review CRITICAL): the BFS
    back-pointer map MUST use a computed key (``prev[a] = null``), not an object literal
    ``{ a: null }`` -- the latter keyed the literal string "a", so the root never got a
    ``null`` sentinel and chain reconstruction never terminated (the tab froze). pytest
    can't run JS, so we pin the corrected *source*: the literal-key form is absent and
    the computed-key sentinel is present."""
    _ingest(client, "map-path", _net_payload())
    body = client.get("/netmap").text
    assert "{ a: null }" not in body  # the buggy literal-key form is gone
    assert "prev[a] = null" in body  # computed-key sentinel is present -> terminates
    # also: ADV results are cached (recomputed only on state change), not per frame
    assert "function recomputeNav()" in body
    assert "navCache.iso = isolateSet()" in body


def test_netmap_time_machine_plaque_on_ssr_route(client):
    """Regression pin for the Ф5 history-plaque divergence (code-review HIGH): the SSR
    ``/netmap?at=<id>`` route must carry ``history_at`` (via the shared
    ``historical_graph_from_snapshot`` normaliser) so the in-canvas time-machine plaque
    renders on the route users actually hit -- not only via the API."""
    from server import db

    graph = {"nodes": [{"nid": "nd-r1", "dev_type": "router", "ip": "10.0.0.1"}], "links": []}
    db.store_topology_snapshot(graph, received_at="2026-06-20T00:00:00+00:00")
    sid = db.list_topology_snapshots(limit=1)[0]["id"]
    g = _embedded_graph(client.get(f"/netmap?at={sid}").text)
    assert g["history_at"] == sid  # the marker the canvas keys the plaque off of
    assert g["received_at"]


def test_netmap_l2_layer_covers_legacy_physical_link_kinds(client):
    """The L2 layer toggle must hide every physical edge, not just current
    ``l2-edge``/``l2-trunk`` names. Historical snapshots can carry legacy kinds like
    ``ethernet``; those still belong to the L2 layer."""
    _ingest(client, "map-l2", _net_payload())
    body = client.get("/netmap").text
    assert "function isL3Link(L)" in body
    assert 'including legacy snapshot kinds such as "ethernet"' in body
    assert "else if (!state.layers.l2) return false" in body


def test_netmap_side_panel_isolate_and_drag_persistence_are_wired(client):
    """Regression pins for two Ф5 interaction bugs: side-panel isolate must recompute
    the cached BFS set after assigning the selected root, and drag end must persist
    the final fixed coordinates instead of clearing ``fx/fy`` before saving."""
    _ingest(client, "map-drag", _net_payload())
    body = client.get("/netmap").text
    assert "state.isolateRoot = n.nid; recomputeNav(); invalidate(); updateNavInfo();" in body
    assert "dragNode.fx = dragNode.x; dragNode.fy = dragNode.y;" in body
    assert "persistPositions();" in body


def test_netmap_node_predicate_is_in_outer_scope_for_pick(client):
    """Regression pin (live bug 2026-06-28): ``nodeFilteredOut`` MUST be declared at the
    engine IIFE scope -- i.e. BEFORE ``function draw`` -- because ``pick()`` (called on
    every mousedown/mousemove) uses it. When it was nested inside ``draw()`` instead,
    every ``pick`` threw ``ReferenceError: nodeFilteredOut is not defined``, which killed
    ALL pointer interaction (select / drag / hover / click-through to the card) even
    though the static render kept working (draw had it in scope). pytest cannot execute
    canvas JS, so we pin the *scope* structurally: a single definition that precedes
    ``draw`` and is referenced by ``pick``."""
    _ingest(client, "map-pred", _net_payload())
    body = client.get("/netmap").text
    i_pred = body.find("function nodeFilteredOut(")
    i_draw = body.find("function draw(")
    i_pick = body.find("function pick(")
    assert i_pred != -1 and i_draw != -1 and i_pick != -1
    # outer scope => the definition appears before draw (not nested inside it)
    assert i_pred < i_draw, "nodeFilteredOut must be declared before draw (outer scope)"
    # pick() relies on it (the call lives in pick's body)
    assert "nodeFilteredOut(n)" in body[i_pick : i_pick + 400]
    # exactly one definition -> no stale shadow copy left inside draw
    assert body.count("function nodeFilteredOut(") == 1


# --- Ф10: «Топология» demolished -> one entry point, /netmap -------------------


def _seed_net_device(nid, ip, hostname, dev_type="switch", status="up"):
    from server import db

    db.upsert_net_device(
        {
            "device_nid": nid,
            "ip": ip,
            "hostname": hostname,
            "mac": "AA-BB-CC-00-00-01",
            "vendor": "Cisco",
            "dev_type": dev_type,
            "status": status,
        }
    )


def test_topology_redirects_to_netmap(client):
    """Ф10: the old /topology page is gone; the route 301-redirects to /netmap so
    any bookmark/external link still lands on the unified map."""
    resp = client.get("/topology", follow_redirects=False)
    assert resp.status_code == 301
    assert resp.headers["location"] == "/netmap"


def test_nav_has_no_topology_link(client):
    """The nav no longer offers «топология» -- the map is the single entry point."""
    body = client.get("/netmap").text
    assert 'href="/topology"' not in body
    assert 'href="/netmap"' in body  # ...but the map link is present


def test_net_device_card_shows_interfaces_and_links(client):
    """The standalone /netdisco/device/{nid} card SURVIVES the demolition (it is the
    network-device detail, reached from the map via card_url)."""
    from server import db

    _seed_net_device("nd-sw", "192.168.1.2", "floor-switch", dev_type="switch")
    _seed_net_device("nd-gw", "192.168.1.1", "core-gw", dev_type="router")
    db.store_net_interfaces(
        "nd-sw",
        [{"if_index": 1, "name": "GigabitEthernet0/1", "if_type": 6, "oper_up": 1}],
    )
    db.upsert_net_link(
        {
            "a_nid": "nd-gw",
            "b_nid": "nd-sw",
            "link_kind": "ethernet",
            "via_source": "lldp",
            "confidence": "high",
        }
    )
    db.store_net_change("device_new", device_nid="nd-sw", detail={"dev_type": "switch"})
    body = client.get("/netdisco/device/nd-sw").text
    assert "floor-switch" in body
    assert "GigabitEthernet0/1" in body  # interface row
    assert "nd-gw" in body or "core-gw" in body  # incident link
    assert "device_new" in body or "появилось" in body  # change journal


def test_net_device_card_404(client):
    assert client.get("/netdisco/device/nope").status_code == 404
