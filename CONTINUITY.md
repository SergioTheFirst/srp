# CONTINUITY.md — Session Continuity Ledger

_Canonical briefing. Survives compaction. Facts only; mark `UNCONFIRMED` if unsure._

## Goal (incl. success criteria)
- Evolve SRP (Windows PC failure early-warning) from MVP toward production per `cctodo.md`.
- Immediate thread: **CLOSE P0 data-integrity foundation (W0.1/W0.2/W0.5/W0.4)** before any analytics (§4/W4.*). Strategy §8: nothing in Analytics is trusted until P0 closed.
- Success: append-only longitudinal history (trends + labels possible); server-stamped time; scores degrade to UNKNOWN/insufficient-data under low coverage (D2 enforced at score level); CONTRACT_VERSION compat tested.

## RE-PLAN (2026-06-01) — strategy-driven, follow to end
- **Finding:** ledger jumped to P1 deployability (W1.x) after W0.3, but P0 §2 items W0.1/W0.2/W0.4/W0.5 remain OPEN (verified in code: `db.py` historical+scores still PK(device_id) overwrite; `pipeline.py` trusts `env.ts`; `scores.py` starts at 100 with no coverage gate). Strategy §8 mandates ALL P0 before Analytics.
- **Decision:** close P0 in dependency order; remaining P1 polish (signed config, transport jitter/idempotency, rate-limit) deferred behind P0 (§3 = "without ceremony", non-blocking).
- **Order this run:** (1) ✅ W0.1 append-only historical+scores [DONE — merge `a6f3de2`] → (2) **W0.2 (NEXT)** server `received_at`+clock-drift → (3) W0.5 confidence-gated scoring → (4) W0.4 CONTRACT_VERSION compat. Each: branch → TDD → gate green → subagent review → `merge --no-ff` → `push origin main` → ledger update. Push if tokens run low.

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
- **Identity-labels (NOT tenancy) — designed+DESCOPED 2026-06-02, DEFERRED behind P0; integrated into `cctodo.md` §3:** org/dept = additive numeric LABELS extending `site_code` (server-side name directory `organizations`/`departments`; client never gets names; contract additive, no version bump; edge `0`/'' → «no dept»/NULL). Onboarding = interactive numbers + `install-service.ps1` args (GPO/SCCM). User confirmed **external-only threat model** → existing global `ingest_token` (merged `b49a738`) is the sufficient anti-spoof boundary. **Data-isolation / per-org secrets / dashboard-auth NOT built** — `cctodo.md` §1 anti-goal «multi-tenant orchestration» + D7; parked until a real 2nd mutually-distrusting customer (D8 keeps the option via additive labels). Storage-isolation Q moot (nothing to isolate — I over-asked). Why → memory `[[identity-labels-not-tenancy]]`.

## State

### Done
- **W0.1 append-only longitudinal history MERGED to `main` (`a6f3de2`):** `historical`+`scores` latest-wins → append-only (`id` AUTOINCREMENT + `(device_id,id)` idx); `store_*` = INSERT + per-device retention prune (`_retain_hist=2000`/`_retain_scores=5000`); `get_historical`/`get_device` latest-by-id; `get_devices` fleet JOIN → `MAX(id)` per device (no row-multiplication); new `get_historical_series`/`get_score_series` (newest-first+limit); **atomic+idempotent in-place migration** of pre-W0.1 DBs (BEGIN/COMMIT rebuild + `DROP IF EXISTS …__new` shadow; lossless ≤1-row/device). 11 tests (`test_db_append_only.py`). Gate green (cov 91.18%, smoke OK); security-reviewed APPROVE-WITH-FIXES → HIGH (migration crash-window) **empirically DISPROVEN** (tx DDL rolls back DROP — verified by abort test), MEDIUM constant-table guard added (`_has_id_column`), MEDIUM "test fails" = FP (test calls `upsert_device`). Accepted-minor: retention = count-cap per device (downsample policy deferred, cctodo W0.1). **NEXT P0 = W0.2.**
- **W0.3 telemetry-trust Plans 1 + 2 + 3 (3a–3e) merged to `main` + pushed** (`main` @ `d13cc72`):
  - **Plan 1** (`server/trust/`, merge `73a2be2`): pure trust core (state/weight, collector⊥semantic, materiality, Tiered domains).
  - **Plan 2** (merge `0e1d3d0`): `SourceHealth`+`Envelope.source_health`; agent `run_ps`→`PsResult`; collectors emit per-source `collector_status`; stdlib-pure.
  - **Plan 3** (merge `1c77ce9`): DB last-good+trust+`device_source_trust`; `evaluate_trust` on ingest (per-source→domain trust, lineage, accumulates across msg types); risk gated by trust (UNKNOWN classes; untrusted device withholds day-1 scores §7); dashboard surfaces trust + coverage.
  - **Plan 3e** (merge `d13cc72`): newly-blocked detection — a source that delivered before and now fails is flagged `regressed` (vs never-seen), surfaced in lineage + dashboard banner. No schema change (derived from `source_last_good` + collector_status).
  - Gates green throughout (coverage 92%); each plan spec + code-quality reviewed via subagent.
- Docs on main: `cctodo.md`, `telemetry-trust-contract.md`, `telemetry-trust-plan.md`. Memory: `system-class-high-trust`, `telemetry-trust-rules`, `working-style-governors`. `.codegraph/` gitignored.

### Now
- **CURRENT (2026-06-03): back on P0 per RE-PLAN — W0.1 append-only ✅ MERGED `a6f3de2`; NEXT = W0.2 server `received_at`+clock-drift. The P1/W1 notes below are prior context, not the active thread.**
- W0.3 COMPLETE. Phase W1: **W1.0 `.claudeignore` ✅** (`b2ffd29`); **W1.1 site/org identity ✅** (`440d447`); **W1.2 cert-expiry ✅ merged** (`cb9555a`: cert metadata in `historical.certificates`, `days_until`, dashboard highlight; security-reviewed APPROVE).
- **W1.3 live dashboard** on `feat/live-dashboard`: **3a backend ✅** (`c78eaab`: enriched `GET /api/v1/devices` [device_trust, unknown_domains, regressed_count, stale+last_seen_age_sec, cert_min_days/cert_expiring, ack] + `acknowledgements` table + `POST /devices/{id}/ack`; gate 92%).
- **W1.3b live dashboard ✅** (`a19fd33`: `_fleet_body.html` partial + `/fleet/fragment`; `fleet.html` shell polls/swaps ~12s; KPI-filter cards, site grouping, search, per-device alert badges, ack-from-list; gate 91%). In **security+quality review** of `feat/live-dashboard` → reviewed APPROVE (4 minor fixes applied: immutable context, ack-note cap, fmt_age guard, live subtitle) and **merged to `main` @ `d77e934`**.
- **PHASE W1 COMPLETE** — W1.0–W1.3 all on `main`, gate green (coverage 90.85%). **P1 deployability — doing ONLY the next item this turn: ingest auth** on `feat/ingest-auth`. Optional shared token (`ingest_token`; empty = disabled, non-breaking): server checks header on `/api/v1/ingest`, agent sends it. **ingest auth ✅ merged** (`b49a738`): server checks `X-SRP-Token` constant-time (`hmac.compare_digest`), agent sends it; empty token = off (non-breaking); security-reviewed APPROVE. **close public-IP default ✅ merged** (`538c7ad`): removed `_DEFAULT_SERVER_URL` public IP → `ClientConfig.server_url=""`; new `ConfigError`+`validate_runtime_config` (raises if empty/whitespace); `agent.main` validates AFTER `--server` override → `SystemExit(2)` on miss (fail-closed); committed `client/config.json` template emptied; README+CHANGELOG updated; `tests/test_config.py` (9 tests incl. exit-path + `--server` rescue). Operator sets `server_url` at install (LAN typical; public IP still a valid explicit choice). Gates green (cov 90.91%, smoke OK); security-reviewed APPROVE-WITH-FIXES → MEDIUM fixed (literal prod IP removed from error string; README keeps it per product req). Accepted-minor: `Transport.__init__` builds `_ingest_url` from unvalidated `server_url` (pre-existing; production path gated by `main`, only direct/test construction affected). **Windows service under LocalSystem ✅ merged** (`1145fb8`): chose **Task Scheduler** (native, zero extra binaries — pure-stdlib loop can't be a real SCM service, pywin32 banned). `client/deploy/install-service.ps1`+`uninstall-service.ps1` (AtStartup, SYSTEM, RunLevel Highest, restart 3×1min, no time limit; install merges `server_url`/site/token→config.json UTF-8 **no-BOM**, validates via one `--once` pass [no `--server`, tests installed path], registers+starts). `agent.py`: `setup_logging` + opt-in stdlib `RotatingFileHandler` (1MB×3) via `--log-file`; startup line logs `user=` (confirm SYSTEM); `server_url` redacted (strip `user:pass@`) before logging. **WHY SYSTEM:** unblocks SMART/StorageReliabilityCounter/WMI collectors (else telemetry-trust → UNKNOWN). README deployment section folds **TLS reverse-proxy note (P1 #2 doc ✅)** + fixed stale "ingest unauthenticated" note. `tests/test_agent_logging.py` (5: logging + URL redaction). Gates green (cov 90.91%, smoke OK); PS scripts AST-parse-clean (Windows-only, not CI-gated); security-reviewed APPROVE-WITH-FIXES → H1(validation `--server` drop)/H3(url redaction)/M4(isinstance)/M5(uninstall warn) applied; H2 verified-safe (COM `-Execute`); M1/M2/M3 non-blocking. design via /grill-me (6 decisions). REMAINING P1 (separate, NOT done): signed config/schema (optional), transport reconnect-jitter + idempotency dedup, server body-size + per-device rate-limit. Accepted-minor: stale `# nosec B105` on empty-token defaults (harmless). Pending: CLAUDE.md "Pasted text #1" re-paste; `CONTINUITY.md` now tracked in git. CLAUDE.md rewritten token-optimized (≤80 lines: codegraph daemon · model routing · subagent-by-default · memory tiers · gates).

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
