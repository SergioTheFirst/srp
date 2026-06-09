# SRP developer tasks. Recipes call the tools directly via `python -m ...`, so
# they behave identically locally and in CI. On Windows, install GNU make
# (`choco install make`) or just run the command shown in each recipe by hand.

PY ?= python
PKGS := server shared client

.PHONY: help install lint format typecheck security test coverage check clean

help:
	@echo "install    install runtime + dev dependencies"
	@echo "lint       ruff lint (style, imports, likely bugs)"
	@echo "format     ruff auto-format + autofix"
	@echo "typecheck  mypy (shared + server + client)"
	@echo "security   bandit static security scan"
	@echo "test       pytest (quick)"
	@echo "coverage   pytest with coverage gate (server + shared, fail_under 80)"
	@echo "check      lint + typecheck + security + coverage  (what CI runs)"
	@echo "clean      remove caches and generated artifacts"

install:
	$(PY) -m pip install -r requirements-dev.txt

lint:
	$(PY) -m ruff check .

format:
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix .

typecheck:
	$(PY) -m mypy

security:
	$(PY) -m bandit -c pyproject.toml -q -r $(PKGS)

test:
	$(PY) -m pytest

coverage:
	$(PY) -m pytest --cov=server --cov=shared --cov-report=term-missing --cov-report=xml

check: lint typecheck security coverage

clean:
	$(PY) -c "import shutil, glob, os; [shutil.rmtree(p, ignore_errors=True) for p in glob.glob('**/__pycache__', recursive=True) + ['.pytest_cache', '.ruff_cache', '.mypy_cache', 'htmlcov']]; [os.remove(f) for f in ('.coverage', 'coverage.xml') if os.path.exists(f)]"
