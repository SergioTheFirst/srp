# SRP Constitution

> Failure early-warning system: thin Windows agent → FastAPI+SQLite server → live dashboard.
> This document is the durable **why** — the principles every spec, plan, and review is judged against.
> `CLAUDE.md` is the operational **how** (routing, gates, tooling) and must stay consistent with it.

## Core Principles

### I. Agent Purity (stdlib-only)
The agent (`client/`) uses pure Python stdlib — zero third-party dependencies
(`urllib`/`subprocess`/`json`/`winreg`/`ipaddress`). Any import from `requirements.txt`
inside `client/` is a bug, not a style issue.
*Rationale: the agent deploys to arbitrary fleet Windows boxes with no pip and no network
egress guarantees; the smallest dependency surface is the smallest attack and maintenance surface.*

### II. Windows PowerShell 5.1 Floor
Every cmdlet and parameter the agent invokes must exist in Windows PowerShell 5.1
(e.g. `Test-Connection` has no `-TimeoutSeconds`). Runtime is bounded via caps and loops,
never PS6+ flags.
*Rationale: stock Windows is the fleet baseline; the agent must run everywhere without installs.*

### III. Language-Independent Collection
Collection parses numbers and English enum names ONLY — never localized text.
CIM `Win32_PerfFormattedData_*` over `Get-Counter`, numeric `$e.Level`, numeric `ifType`.
*Rationale: the fleet runs localized Windows; text parsing breaks silently and per-locale.*

### IV. Privacy by Construction
Disk serials are SHA-256-hashed inside the agent; the raw serial never leaves the machine.
Certificates ship metadata only — never private keys. Only RFC1918 addresses leave the agent;
active network probing stays RFC1918-gated, hard-capped, and owner-authorized.
*Rationale: telemetry must be safe to centralize; secrets and PII must never be.*

### V. Server Trust Boundary
Agents are untrusted input. pydantic v2 validates at the boundary (`shared/schema.py` is the
contract; new fields are additive-optional — no CONTRACT_VERSION bump). ALL SQL is parameterized.
Jinja2 autoescape stays ON (no `|safe`); agent-origin strings pass `srpEsc` before any
client-side JS sink.
*Rationale: a compromised or buggy agent must not be able to injure the server or the operator's browser.*

### VI. Honest Uncertainty (trust engine)
In `server/trust/`: `state` is a gate, `weight` is modulation — weight never revives a
gate-failed source. Collector trust is orthogonal to semantic trust. **UNKNOWN over false
confidence.** Semantic validation only for decision-material signals; bayesian weights are
uncalibrated and advisory.
*Rationale: in an early-warning system a wrong-but-confident score is worse than no score.*

### VII. Bilingual Surface Discipline
Operator-facing prose (dashboard, engine factors/reasons/missing_evidence) is Russian;
technical terms stay Latin (RSI/BSOD/SMART/KP41). Machine values (enums, lineage keys,
trust states, bands, confidence) are English — tests pin this split.
*Rationale: operators read Russian; code, tests, and integrations need stable identifiers.*

### VIII. Test-Verified Done
TDD per task: test RED → implement GREEN → refactor. Before merge the FULL gate is green:
ruff · mypy (server+shared+client) · bandit · pytest cov ≥80% · `python smoke.py`.
Green is never claimed unverified. Security review is mandatory for changes touching
agent/PowerShell/ingest/SQL/certificates/privacy.
*Rationale: an unverified early-warning system is itself a failure waiting to be discovered late.*

### IX. No Dormant Features
Anything OFF-by-default in code ships ON in `server/config.json` (or dashboard-toggleable)
in the SAME change. Code keeps the secure default and stop-gate; deployment opts in.
Credential-gated integrations (operator secrets required) are the sanctioned exception.
*Rationale: owner rule 2026-06-21 — shipped-but-dormant code is untested code and unrealized value.*

### X. Simplicity Floor
Python 3.9 floor, explicit `Optional`, line length 100, double quotes. Immutable patterns;
files <800 lines, functions <50; early returns over nesting; no speculative abstraction —
reuse an existing helper before writing a new one.
*Rationale: the fleet outlives any one contributor; boring code is the code that gets maintained.*

## Delivery Workflow

- Branch-first → full gate green → subagent review → `merge --no-ff` → `push origin main` —
  all automatic, never ask (owner rule 2026-06-21). Conventional commits, no attribution.
- Stage ONLY files touched for the change — never `git add -A`. Local `client/config.json`
  and `org_directory.json` are never committed (templates stay empty).
- Visible behavior change → `CHANGELOG.md` `[Unreleased]` line in the same commit.
  `CONTINUITY.md` updated every working turn. Durable decisions → `.claude/memory/` (one fact per file).
- Big or ambiguous change → design first: spec + plan in `docs/superpowers/` before code.

## Governance

- This constitution supersedes ad-hoc practice for principles; `CLAUDE.md` governs day-to-day
  operations and MUST NOT weaken a principle stated here.
- Amendment = a commit editing this file with a version bump: MAJOR — principle removed or
  reversed; MINOR — principle added or materially expanded; PATCH — wording/clarification.
- Compliance is checked at the review gates of Principle VIII; a violation blocks merge.

**Version**: 1.0.0 | **Ratified**: 2026-07-02 | **Last Amended**: 2026-07-02
