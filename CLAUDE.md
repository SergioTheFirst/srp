# CLAUDE.md — SRP (failure early-warning: thin Windows agent → FastAPI+SQLite server → live dashboard)
> Auto-loaded every turn → keep ≤80 lines, MUST not advice. Deep specs (§4) read on-demand, never auto-read.

## 0 · Token discipline (non-negotiable)
- Never read a whole file for one fact → locate via §1, read only that span.
- Main thread = conclusions + the edit; fan-out → subagents (§3) return verdicts, not dumps.
- One fact = one source of truth; never restate state across memory files (§4).
- While iterating run ONLY the touched tests (`pytest tests/test_x.py -q`); the FULL gate (§6) once, before commit.

## 1 · `.codegraph/` = navigation source of truth
- A daemon keeps `.codegraph/` fresh (symbols/callers/deps/file-map). GENERATED: never edit, gitignored, never commit.
- ANY "where defined / what calls / imports / which module" → query `.codegraph/` FIRST; Glob/grep only on a miss.
- If `.codegraph/` mtime predates your last edit → re-read that symbol from disk before trusting it.

## 2 · Model & effort routing — dynamic, cost-first (classify EVERY task; take the CHEAPEST row that fully covers it)
| Row | SRP task archetypes | Model · effort |
|---|---|---|
| R1 | fact/Q&A · rename/format · gate-fix (E501/mypy/import) · CHANGELOG/ledger/doc line · RU-string tweak · 1-file deterministic patch | Haiku · low |
| R2 | execute ONE task of an APPROVED plan · tests for existing behavior · bugfix WITH repro · dashboard/template tweak · review a routine diff | Sonnet · medium |
| R3 | new collector/engine/endpoint on an existing template (print_jobs · W4.2 engines · certificates-fold) · multi-file change · bug w/o repro | Opus · high |
| R4 | `shared/schema.py` (contract) · `server/trust/` · scoring weights/bands/gating · agent PowerShell/privacy · ingest/auth/SQL surface · spec/plan/decomposition · ambiguous ask | Opus · max |
- Dynamic: (a) design at R4 → EXECUTING its approved plan-tasks drops to R2 each; (b) escalate +1 row when unsure, >3 files, or after 2 failed attempts at the current row; (c) R4 triggers NEVER de-escalate — inside any plan those steps run ≥R3 + security-review.
- Applies to subagent `model` AND main-thread `/effort`. Fan-out searches → Haiku; deep code-explain → Sonnet. Fast Mode (`/fast`) speeds Opus output — it saves time, not tokens; cost cuts come from the Haiku/Sonnet rows.

## 3 · Subagents = delegate by default
- DEFAULT = spawn; main thread keeps decision + edit + merge. Independent tasks → parallel in ONE message.
- search / explore / verify / review spanning >1 file → subagent (Explore=search · general-purpose=multi-step+TDD · Plan=design), model per §2.
- Reviews ALWAYS via subagent before merge: `security-reviewer` (Opus) mandatory for agent/PowerShell/ingest/SQL/cert/privacy; `code-reviewer` (Sonnet) otherwise. Skip a subagent ONLY for a single known-symbol lookup or a trivial edit.

## 4 · Project memory — write to EXACTLY one
| File | Role | Write when |
|---|---|---|
| CLAUDE.md | static rules (this) | a rule changes |
| CONTINUITY.md | LIVE ledger (goal/decisions/state); survives compaction | read at top of EVERY turn, then update |
| .claude/memory/ | durable invariants: `MEMORY.md` index + 1 fact/file, `[[links]]` | learned a non-obvious decision/why |
| CHANGELOG.md | user-visible changes, `## [Unreleased]` (Keep a Changelog) | same commit as a visible-behavior change |
- `cctodo.md` (roadmap) · `telemetry-trust-contract.md`/`-plan.md` · `docs/superpowers/` specs+plans = read on-demand; never auto-read, never duplicate into memory.

## 5 · Hard invariants (MUST; never weaken)
- Agent `client/` = pure stdlib, ZERO deps (urllib/subprocess/json/winreg/ipaddress). Any `requirements.txt` import = bug. `[[agent-stdlib-only]]`
- Agent PS = **Windows PowerShell 5.1 floor**: every cmdlet/param must exist in 5.1 (`Test-Connection` has no `-TimeoutSeconds`); bound runtime via caps/loops, never PS6+ flags. `[[agent-powershell-51-floor]]`
- Language-independent collection: CIM `Win32_PerfFormattedData_*` (not Get-Counter), numeric `$e.Level`, numeric `ifType`; parse numbers + English enum names ONLY, never localized text. `[[language-independence]]`
- Privacy: disk serials SHA-256 in agent; raw serial never leaves it. Certificates = metadata, NEVER private keys. Network: only RFC1918 addresses leave the agent.
- Server: Jinja2 autoescape ON (no `|safe`); ALL SQL parameterized; pydantic v2 validates at the boundary (`shared/schema.py` = contract; new fields additive-optional → no CONTRACT_VERSION bump).
- Trust (`server/trust/`): `state`=gate, `weight`=modulation (never revives a gate-failed source); collector⊥semantic; **UNKNOWN over false confidence**; semantic-validate only decision-material signals; bayesian weights uncalibrated. `[[bayesian-weights-uncalibrated]]`
- Operator-facing prose (dashboard, engine factors/reasons/missing_evidence) = Russian; tech terms stay Latin (RSI/BSOD/SMART/KP41); machine values (enums, lineage keys, trust states, band/confidence) = English — tests pin this.
- Python 3.9 floor: explicit `Optional` (UP off), line 100, double quotes; `# nosec <code>` only with a reason. Immutable; files <800 / funcs <50; early returns.
- PostToolUse hook runs `ruff --fix`+`format` on each `.py` edit (strips not-yet-used imports → add an import WITH its first use; accept its formatting).

## 6 · Process + "Done" gates (verify; never claim green unverified)
- Big/ambiguous change → design first at R4 (brainstorm/Plan → spec+plan in `docs/superpowers/`) → TDD per task (test RED→GREEN) → subagent review → fix. Invoke the matching skill BEFORE coding.
- Git: branch-first → gate green → `merge --no-ff` → push only when asked; conventional commits, NO attribution. **Auto-commit each important/complete change WITHOUT asking** (user directive 2026-06-10). NEVER `git add -A`/sweep — stage ONLY files touched for THIS change; local `client/config.json` values never committed (template stays empty).
- GATES before "done"/merge: `make check` = ruff · mypy[server+shared+client] · bandit · pytest cov ≥80% (no `make` on this box → run the `python -m …` recipes from Makefile) ALL GREEN · `python smoke.py` OK · visible change → CHANGELOG line in the same commit · CONTINUITY.md updated.
