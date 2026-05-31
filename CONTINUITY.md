# CONTINUITY.md — Session Continuity Ledger

_Canonical briefing. Survives compaction. Facts only; mark `UNCONFIRMED` if unsure._

## Goal (incl. success criteria)
- Evolve SRP (Windows PC failure early-warning) from MVP toward production per `cctodo.md`.
- Immediate thread: **telemetry-trust subsystem (W0.3)** — the P0 data-integrity foundation.
- Success: built to `telemetry-trust-contract.md` (§14); trustworthy data layer; scoring can return UNKNOWN.

## Constraints/Assumptions
- System class: **high-trust degradation detection platform**, NOT "AI predicts failures". Prime directive: under uncertainty → UNKNOWN, never guess.
- Scope = **Phased/gated**: ML/survival/labels deferred (not killed). Deployable-but-not-deployment-driven; telemetry-trust = absolute P0.
- **Scope ceiling**: no nested confidence / evidence DAGs / confidence calculus. Stop at state/weight/collector_trust/semantic_trust/freshness.
- Thin agent invariant: `client/` pure stdlib, zero deps. Language-independent collection (CIM classes, numeric event Level). Serial hashed in agent.
- Python 3.9 floor, explicit `Optional`, line-length 100, double quotes. Immutable patterns, files <800 / funcs <50 lines.
- `make check` green before every code commit (lint/mypy/bandit/coverage≥80%). CHANGELOG line for visible-behavior changes. No commit attribution.
- Git: branch-first → `merge --no-ff` → `push origin main`. `.codegraph/` gitignored. Auto-format hook (`ruff --fix`+`format`) runs on every `.py` edit (strips not-yet-used imports; collapses comment alignment — accept it).
- User authorized continuous execution + all run/subagent permissions; maintain this ledger every turn; use subagents + skill where viable (note: large subagent dispatches repeatedly hit session limits → implementation often done directly, reviews via subagent).

## Key decisions
- `state` = authoritative gate; `weight` = modulation only (never reanimates a gate-failed source).
- `collector-trust ⊥ semantic-trust`: agent reports collection facts; server judges plausibility (A1).
- **Materiality governor**: semantic validation only for decision-material signals; thermal = frozen-check only.
- **UNKNOWN** first-class outcome; untrusted identity → device scores withheld (contract §7).
- F3 v1 validators: stateless (range/cross-field/known-bad) + frozen-on-last-good; trend-based deferred.
- **3c mapping (decided):** bayesian CLASS→trust DOMAIN gate — storage→storage, battery→battery, power_thermal→thermal, stability→os_stability; **memory ungated**; disk_fill/boot tracked but gate no class (v1).
- **3e:** reuse `source_last_good` — a source with a stored last-good that now reports a collector failure = **regressed (newly-blocked)**; flag in lineage + dashboard. Distinguishes "was capable, now lost it" from "never seen".
- **W1 design (decided, governor-driven):** `site_code` = grouping/identity only (no auth/isolation now); certs folded into `historical` (no new msg_type), never read private keys, surfaced directly (not a bayesian class); dashboard = vanilla-JS poll (no SPA/websocket); feedback = ack+note only (no remote agent control). Each piece justified; nothing speculative.

## State

### Done
- **W0.3 telemetry-trust Plans 1 + 2 + 3 (3a–3e) merged to `main` + pushed** (`main` @ `d13cc72`):
  - **Plan 1** (`server/trust/`, merge `73a2be2`): pure trust core (state/weight, collector⊥semantic, materiality, Tiered domains).
  - **Plan 2** (merge `0e1d3d0`): `SourceHealth`+`Envelope.source_health`; agent `run_ps`→`PsResult`; collectors emit per-source `collector_status`; stdlib-pure.
  - **Plan 3** (merge `1c77ce9`): DB last-good+trust+`device_source_trust`; `evaluate_trust` on ingest (per-source→domain trust, lineage, accumulates across msg types); risk gated by trust (UNKNOWN classes; untrusted device withholds day-1 scores §7); dashboard surfaces trust + coverage.
  - **Plan 3e** (merge `d13cc72`): newly-blocked detection — a source that delivered before and now fails is flagged `regressed` (vs never-seen), surfaced in lineage + dashboard banner. No schema change (derived from `source_last_good` + collector_status).
  - Gates green throughout (coverage 92%); each plan spec + code-quality reviewed via subagent.
- Docs on main: `cctodo.md`, `telemetry-trust-contract.md`, `telemetry-trust-plan.md`. Memory: `system-class-high-trust`, `telemetry-trust-rules`, `working-style-governors`. `.codegraph/` gitignored.

### Now
- W0.3 COMPLETE. Phase W1: **W1.0 `.claudeignore` ✅** (`b2ffd29`); **W1.1 site/org identity ✅** (`440d447`); **W1.2 cert-expiry ✅ merged** (`cb9555a`: cert metadata in `historical.certificates`, `days_until`, dashboard highlight; security-reviewed APPROVE).
- **W1.3 live dashboard** on `feat/live-dashboard`: **3a backend ✅** (`c78eaab`: enriched `GET /api/v1/devices` [device_trust, unknown_domains, regressed_count, stale+last_seen_age_sec, cert_min_days/cert_expiring, ack] + `acknowledgements` table + `POST /devices/{id}/ack`; gate 92%).
- **W1.3b live dashboard ✅** (`a19fd33`: `_fleet_body.html` partial + `/fleet/fragment`; `fleet.html` shell polls/swaps ~12s; KPI-filter cards, site grouping, search, per-device alert badges, ack-from-list; gate 91%). In **security+quality review** of `feat/live-dashboard` → reviewed APPROVE (4 minor fixes applied: immutable context, ack-note cap, fmt_age guard, live subtitle) and **merged to `main` @ `d77e934`**.
- **PHASE W1 COMPLETE** — W1.0–W1.3 all on `main`, gate green (coverage 90.85%). **P1 deployability — doing ONLY the next item this turn: ingest auth** on `feat/ingest-auth`. Optional shared token (`ingest_token`; empty = disabled, non-breaking): server checks header on `/api/v1/ingest`, agent sends it. **ingest auth ✅ merged** (`b49a738`): server checks `X-SRP-Token` constant-time (`hmac.compare_digest`), agent sends it; empty token = off (non-breaking); security-reviewed APPROVE. **close public-IP default ✅ merged** (`538c7ad`): removed `_DEFAULT_SERVER_URL` public IP → `ClientConfig.server_url=""`; new `ConfigError`+`validate_runtime_config` (raises if empty/whitespace); `agent.main` validates AFTER `--server` override → `SystemExit(2)` on miss (fail-closed); committed `client/config.json` template emptied; README+CHANGELOG updated; `tests/test_config.py` (9 tests incl. exit-path + `--server` rescue). Operator sets `server_url` at install (LAN typical; public IP still a valid explicit choice). Gates green (cov 90.91%, smoke OK); security-reviewed APPROVE-WITH-FIXES → MEDIUM fixed (literal prod IP removed from error string; README keeps it per product req). Accepted-minor: `Transport.__init__` builds `_ingest_url` from unvalidated `server_url` (pre-existing; production path gated by `main`, only direct/test construction affected). REMAINING P1 (separate, NOT done): TLS (reverse-proxy doc), Windows service under SYSTEM, transport reconnect-jitter + idempotency dedup, server body-size + per-device rate-limit. Accepted-minor: stale `# nosec B105` on empty-token defaults (harmless). Pending: CLAUDE.md "Pasted text #1" re-paste; `CONTINUITY.md` now tracked in git. CLAUDE.md rewritten token-optimized (≤80 lines: codegraph daemon · model routing · subagent-by-default · memory tiers · gates).

### Next — Phase W1 (scalable remote monitoring)
- **W1.1 Site/org identity:** `ClientConfig.site_code` (+ optional `site_name`), manually assigned per deployment; `Envelope.site_code/site_name` (additive, forward-compat); `devices` table cols (COALESCE on touch so heartbeat doesn't wipe); fleet grouped/filterable by site. Grouping only — NOT auth/tenancy isolation (defer to prod P1).
- **W1.2 Certificate-expiry:** new agent PowerShell collector (`Cert:\LocalMachine\My` + `CurrentUser\My`) → subject/issuer/thumbprint/not_after/not_before; folded into `historical` payload (`certificates: list[CertInfo]`) — no new msg_type; `certificates` source_health (no bayesian class); server derives days-to-expiry; dashboard highlights soon-expiring (<30d red). NEVER read private keys (stdlib agent unchanged otherwise).
- **W1.3 Live dashboard + feedback:** vanilla-JS poll of `/api/v1/devices` (~10–15s) — no SPA/websocket; site-grouped + search/filter; KPIs (at-risk / UNKNOWN / expiring-certs / regressed / stale); staleness flag (agent silent > N×interval); operator feedback = per-device ack + note (new endpoint + table) — NOT remote agent control (scope ceiling). Use `frontend-design`.
- Then broader `cctodo.md` P1 (ingest auth/TLS, Windows service) when deploying.
- Pending: CLAUDE.md "Pasted text #1" re-paste (never arrived).
- Accepted-minor: `_num` bool→1.0 coercion; `partial` dual-meaning; storage validates first disk only (v1).

## Open questions (UNCONFIRMED if needed)
- `UNCONFIRMED`: CLAUDE.md "Pasted text #1" (~9 lines) never arrived — need re-paste before applying.

## Working set (files/ids/commands)
- Repo: `github.com/SergioTheFirst/srp.git`, `main` @ `1c77ce9`; working branch `feat/trust-capability`.
- Trust code: `server/trust/*`, `server/pipeline.py` (`evaluate_trust`/`recompute_scores`), `server/db.py` (trust tables), `server/web/templates/device.html`, `shared/schema.py`, `client/collectors/*`.
- Commands: `make check`; `python -m pytest -q`; git branch→commit→`merge --no-ff`→`push origin main`.
- Untracked (by design): `.claudeignore`, `CONTINUITY.md`.
