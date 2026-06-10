# CLAUDE.md — SRP (failure early-warning: thin Windows agent → FastAPI+SQLite server → live dashboard)
> Auto-loaded every turn → keep ≤80 lines, MUST not advice. Deep specs (§4) read on-demand, never auto-read.

## 0 · Token discipline (non-negotiable)
- Never read a whole file for one fact → locate via §1, read only that span.
- Main thread = conclusions + the edit; fan-out → subagents (§3) return verdicts, not dumps.
- One fact = one source of truth; never restate state across memory files (§4).

## 1 · `.codegraph/` = navigation source of truth
- A daemon keeps `.codegraph/` fresh (symbols/callers/deps/file-map). GENERATED: never edit, gitignored, never commit.
- ANY "where defined / what calls / imports / which module" → query `.codegraph/` FIRST; Glob/grep only on a miss.
- If `.codegraph/` mtime predates your last edit → re-read that symbol from disk before trusting it.

## 2 · Model routing (classify task → pick model + effort; enforce strictly)
| Task | Model | Effort |
|---|---|---|
| Q&A, format/rename, grep/log triage, doc/CHANGELOG line, 1-file deterministic patch | Haiku 4.5 | low |
| Routine dev in a known pattern: CRUD, write tests, refactor, bugfix w/ repro, review one diff | Opus · Fast Mode | medium |
| New feature, multi-file change, complex/subtle bug, module design | Opus | high |
| Architecture, ambiguous spec, contract/security/scoring change, the decomposition itself, research/novel | Opus | max |
- Rule: pick the LOWEST row that fully covers the task; ambiguous ∨ cross-module ∨ touches contract/security/scoring → `max`; unsure → +1 row. Fast Mode = Opus w/ faster output, no quality downgrade (`/fast`); set reasoning depth via `/effort`.

## 3 · Subagents = delegate by default (almost always)
- DEFAULT = spawn a subagent; main thread keeps only the decision + the edit + the merge.
- search / explore / verify / review spanning >1 file → subagent (Explore=search · general-purpose=multi-step+TDD · Plan=design). Independent tasks → parallel in ONE message.
- Reviews ALWAYS via subagent: review every diff; `security-reviewer` for agent/PowerShell/ingest/SQL/cert. Skip a subagent ONLY for a single known-symbol lookup or a trivial edit.

## 4 · Project memory — write to EXACTLY one
| File | Role | Write when |
|---|---|---|
| CLAUDE.md | static rules (this) | a rule changes |
| CONTINUITY.md | LIVE ledger (goal/decisions/state); survives compaction | read at top of EVERY turn, then update |
| .claude/memory/ | durable invariants: `MEMORY.md` index + 1 fact/file, `[[links]]` | learned a non-obvious decision/why |
| CHANGELOG.md | user-visible changes, `## [Unreleased]` (Keep a Changelog) | same commit as a visible-behavior change |
- `cctodo.md` (roadmap) · `telemetry-trust-contract.md`/`-plan.md` (specs) = read on-demand; never auto-read, never duplicate into memory.

## 5 · Hard invariants (MUST; never weaken)
- Agent `client/` = pure stdlib, ZERO deps (urllib/subprocess/json/winreg). Any `requirements.txt` import = bug. `[[agent-stdlib-only]]`
- Language-independent collection: `Win32_PerfFormattedData_*` CIM (not Get-Counter), numeric `$e.Level` (not LevelDisplayName). `[[language-independence]]`
- Privacy: disk serials SHA-256 in agent; raw serial never leaves it. Certificates = metadata only, NEVER private keys.
- Server: Jinja2 autoescape ON (no `|safe`); ALL SQL parameterized; validate at the boundary via pydantic v2 (`shared/schema.py` = contract).
- Trust (`server/trust/`): `state`=gate, `weight`=modulation (never revives a gate-failed source); collector⊥semantic; **UNKNOWN over false confidence**; semantic-validate only decision-material signals; bayesian weights uncalibrated. `[[bayesian-weights-uncalibrated]]`
- Python 3.9 floor: explicit `Optional` (UP off), line 100, double quotes; `# nosec <code>` only with a reason. Immutable; files <800 / funcs <50; early returns.
- PostToolUse hook runs `ruff --fix`+`format` on each `.py` edit (strips not-yet-used imports → add an import WITH its first use; accept its formatting).

## 6 · Process + "Done" gates (verify; never claim green unverified)
- Big/ambiguous change → design first (brainstorm/Plan) → TDD (test RED→GREEN) → subagent review → fix. Invoke the matching skill BEFORE coding.
- Git: branch-first → gate green → `merge --no-ff` → `push origin main`; conventional commits, NO attribution. **Auto-commit after each important/complete change WITHOUT asking** (user directive 2026-06-10); push only when asked. NEVER `git add -A`/sweep — stage ONLY files you touched for THIS change; never commit unrelated working-tree edits.
- GATES before "done"/merge: `make check` (ruff · mypy[server+shared] · bandit · pytest cov ≥80%) ALL GREEN · `python smoke.py` OK · visible change → CHANGELOG line in the same commit.
