# AGENTS.md — SRP

Failure early-warning system: thin Windows agent (`client/`, pure stdlib) → FastAPI+SQLite server (`server/`) → live dashboard. Full agent rules: [CLAUDE.md](CLAUDE.md) (read first). Live work ledger: `CONTINUITY.md`.

## Toolchain
Python 3.9 floor, pip only: `pip install -r requirements-dev.txt`. No `make` on this box — run the `python -m …` recipes below (they mirror the Makefile and CI).

## Commands
| Task | Command |
|---|---|
| Lint | `python -m ruff check path/to/file.py` |
| Format | `python -m ruff format path/to/file.py` |
| Typecheck | `python -m mypy` (configured for shared + server + client) |
| Security scan | `python -m bandit -c pyproject.toml -q -r server shared client` |
| One test file (while iterating) | `python -m pytest tests/test_x.py -q` |
| Coverage gate | `python -m pytest --cov=server --cov=shared --cov-report=term-missing` (fail_under 80) |
| Smoke | `python smoke.py` |

Full pre-merge gate = lint + typecheck + security + coverage + smoke, ALL green. Never claim green unverified.

## Layout
- `client/` — Windows agent: **pure stdlib, zero deps** (urllib/subprocess/json/winreg/ipaddress); PowerShell snippets must run on Windows PowerShell 5.1
- `server/` — FastAPI + SQLite; `server/trust/` is contract-sensitive — design-level care required
- `shared/schema.py` — pydantic v2 wire contract; new fields additive-optional only
- `tests/` — pytest; run only touched tests while iterating, full gate before commit

## Hard invariants (never weaken)
- ALL SQL parameterized; Jinja2 autoescape ON, no `|safe`
- Language-independent collection: numeric CIM values / English enum names only, never localized text
- Privacy: disk serials SHA-256-hashed in agent; only RFC1918 addresses leave the agent; certificates = metadata, never private keys
- Operator-facing prose = Russian (tech terms stay Latin); machine values (enums, states, keys) = English — tests pin this
- Explicit `Optional` (no `X | None`), line length 100, double quotes; files <800 lines, funcs <50, early returns
- Local `client/config.json` / `org_directory.json` are never committed (templates stay empty)

## Git
- Conventional commits (`feat:`/`fix:`/`refactor:`…), messages in Russian, **no AI attribution lines**
- Branch-first; stage only files touched for this change — never `git add -A`
- Visible behavior change → `CHANGELOG.md` line under `## [Unreleased]` in the same commit
