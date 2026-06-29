# Netmap Enrichment — pruned plan (surface already-collected data on the map)

> Thread: 2026-06-29 NETMAP-ENRICHMENT. Follows netmap-unification (10 phases, done).
> Owner ask: make found network objects more detailed + visible on the existing map,
> maximize already-collected DB fields, no knowledge duplication, avoid overengineering.

## Thesis (kept from owner's plan)
- **No new collectors.** Agent contract frozen (`shared/schema.py` untouched, no CONTRACT_VERSION bump).
- **≤1 new table.** Only `net_routes` (additive, no schema-bump on the contract).
- **Read-already-collected:** ~dozens of fields are persisted then dropped on the way to the map.
- **Derived, not collected:** anything computable from the graph/ip (articulation points, subnet prefixes) is computed, never collected.
- **Self-cleaning:** every node carries age (first_seen/last_seen); stale fades, it does not accumulate as ghosts.
- **All new layers ON for base nav (freshness/changes) or toggle (risk/L3)** per `[[no-disabled-by-default]]`.

## Verification (claims checked against code before trusting the plan)
| Claim | Verdict | Evidence |
|---|---|---|
| `evidence.collect_lldp_mgmt` written, RFC1918-gated, 0 call-sites | TRUE | `server/netdisco/evidence.py:211` + tests only; CONTINUITY Ф7 carry-forward confirms "НЕ подключён" |
| `harvest_routes` returns (cidr,next_hop,ifindex); call-site drops 2/3 | TRUE | `server/netdisco/scheduler.py:132` `(next_hop, None) for _cidr, next_hop, _ifx` |
| `network_connections` has no server reader | TRUE-ish | only a COUNT in `device.html:633`; never aggregated into graph |
| `graph.py` has no articulation/bridge fn | TRUE | 0 matches in `server/` |
| S5 source `net_device_readings` "ignored table" | PLAN RIGHT, LEDGER STALE | writer EXISTS `db.py:933` (added post-P11) → S5 has data, viable |
| S3 source `net_interfaces` not read by assembler | TRUE (precise) | columns+writer exist (`db.py:254-263, 957-970`); card renders them (`net_device.html:60-63`); the **unified assembler** does not JOIN them into edges |

## Pruned scope

### KEEP (genius, data verified)
S1 freshness ring + fade (merge of S1+D1) · S3 edge physics · S4 articulation/bridges ·
S2 change-overlay · S5 reachability sparkline+flaps · D2 last-visit ·
A1 lldp-mgmt wire-up · A2 net_routes + L3 overlay ·
B7 confirmed-SNMP badge · B8 clock-drift flag · B6 subnet loss% · B3 printer-supply badge · B2 wifi-signal.

### CUT / DEFER (with reason)
- A3 gateway-persistence — active-scan already discovers the gateway; touching classify/ingest for a ghost that becomes a node anyway = low payoff / real risk. **CUT.**
- A4 traffic-L4 — biggest, noisiest, OFF-by-default, not "real state". **DEFER to last.**
- B5 site grouping — single-site assumption; do only if owner confirms multi-site. **DEFER.**
- B9 ack, C3 subnet-tree, C4 time-lapse — polish; C4 may overlap Ф5 time-machine. **DEFER.**
- C1 compound nodes — subnet rings already group; duplicate. **CUT.**
- C2 edge-bend — only needed once L3+traffic create parallel edges (after A2). **CONDITIONAL.**
- B1 DNS-anomaly, B4 subtype-glyph — optional micro; fold into a badges PR only if cheap.

### IMPROVE (changes to owner's plan)
1. Merge S1+D1 into one node-lifecycle visual (age ring + missing-fade) — same system, no duplicate item.
2. Order by RISK: pure read-side first (code-review only) → the single SQL table A2 isolated (mandatory security-review) → ingest-touching items cut/deferred. Matches CLAUDE.md §2 routing.
3. Python via real TDD; canvas-JS is not unit-testable (CONTINUITY lesson) → each sprint = Python-first TDD, then a canvas batch verified live with Playwright.
4. The 9 micro-badges become ONE "node-truth badges" PR (B7+B8+B6 first), not 9 trickles.
5. Stale nodes fade but are NOT hidden by default (toggle to hide). Honesty: dim > vanish.

## Sprints (each: branch → TDD → gate green → subagent review → merge --no-ff → push)

### Sprint 1 — "map shows the truth" (read-side, code-review)
- **S1 freshness/fade.** `unified.py`: each node += `first_seen`,`last_seen`,`stale` (bands <5m/<1h/<1d/>1d from net_devices+devices last_seen). `_netgraph.html`: freshness ring + "hide stale" toggle (default: fade, not hide). ON.
- **S3 edge physics.** `unified.py::_real_links`: JOIN `net_interfaces` by (nid, if_index=a_if/b_if) → `speed_mbps`(thickness), `if_alias`(label when a_port/b_port empty), `oper_up`(down-on-live badge). `_netgraph.html`: width + tooltip + down badge. ON.
- **S4 chokepoints.** `graph.py`: pure `find_articulation_points()` + `find_bridges()` (Tarjan, no NetworkX) over existing adjacency. `unified.py`: mark nodes/links. `_netgraph.html`: "risk" layer (OFF, toggle).
- Tests: graph algo (unit), assembler enrich (unit), web string-pins; live Playwright after canvas batch.

### Sprint 2 — "what changed / what flaps" (read-side, code-review)
- **S2 change-overlay** from `net_changes` (already SSR): appeared=pulse, disappeared(missing)=ghost auto-fade 7d, reclassified=blink, link_added/removed=dashed. ON.
- **S5 sparkline + flaps** from `net_device_readings.status` (writer at db.py:933): 24h reachability mini-series + up↔down flap count in tooltip.
- **D2 last-visit** (localStorage): highlight first_seen > last_visit_ts.
- **Node-truth badges:** B7 confirmed (`dev_type≠unknown ∧ sys_object_id≠NULL`) · B8 clock-drift (`devices.clock_drift_sec`, |drift|>30s) · B6 subnet loss% in ring tooltip.

### Sprint 3 — "wire the written + L3" (SQL/ingest → security-review mandatory)
- **A1** call `evidence.collect_lldp_mgmt` in `reconcile.run_topology_cycle` after evidence; feed mgmt-IPs as discovery candidates (no ping-scan; RFC1918 gate already inside).
- **A2** `net_routes(device_nid, cidr, next_hop, ifindex, first_seen, last_seen)` additive table + writer (persist the dropped triple at scheduler.py:132) + reader + `unified` L3 overlay layer (toggle `l3`, already in UI).
- **B3** printer supply/error badge from `printer_readings.detail` (last reading) · **B2** wifi-signal color from `signal_pct`.

### Later / optional
A4 traffic-L4 · C2 edge-bend (after A2) · C3 subnet hierarchy · C4 time-lapse (verify Ф5 first) · B1/B4.

## Invariants honored
- §5: agent stdlib-only untouched; Jinja autoescape ON; new SQL parameterized; A2 additive-optional (no bump).
- Operator prose RU; machine values (states/keys/bands) English — tests pin.
- Files <800 / funcs <50; immutable; Python 3.9 floor, line 100, double quotes.
- §6 gates before each merge: ruff·mypy·bandit·pytest cov≥80%·smoke·CHANGELOG·CONTINUITY.
- security-review mandatory for Sprint 3 (A1/A2 = ingest/SQL); code-review for Sprints 1–2.
