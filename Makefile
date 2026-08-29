# Harbor — development task runner.
#
# Every recipe goes through $(PYTHON) so the interpreter can be pinned per
# environment; the GitHub workflows rely on this (`make test PYTHON=python`).
PYTHON ?= python3

# Harbor uses a src/ layout and tests run against the working tree rather than
# an installed copy, so PYTHONPATH has to point at src/ for every pytest call.
PYTEST := PYTHONPATH=src $(PYTHON) -m pytest

# Bare `make` prints the target list instead of running whatever happens to be
# defined first.
.DEFAULT_GOAL := help

# .PHONY is declared per section, next to the targets it covers.  One combined
# list at the top of the file drifts silently — test-e2e was missing from it
# from the day the e2e suite landed until this refactor.

# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

.PHONY: help
help:  ## Show this help
	@# [0-9] in the pattern matters: without it every target whose name contains a
	@# digit (test-e2e, e2e-setup) is silently omitted from the listing.
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-13s\033[0m %s\n", $$1, $$2}'

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

.PHONY: dev e2e-setup

dev:  ## Install in development mode with dev extras (requires pip)
	@# --break-system-packages first for PEP 668 environments (Homebrew, distro
	@# Python); the retry covers older pips that reject the flag outright.
	$(PYTHON) -m pip install -e ".[dev]" --break-system-packages 2>/dev/null || $(PYTHON) -m pip install -e ".[dev]"

e2e-setup:  ## Install the Chromium build Playwright drives (pip can't manage it)
	@# Idempotent and offline once installed (~0.5s), so test-e2e depends on it:
	@# `make dev` alone leaves the browser missing and the e2e run fails.
	$(PYTHON) -m playwright install chromium

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

.PHONY: run

run:  ## Run Harbor (current directory)
	PYTHONPATH=src $(PYTHON) -m harbor

# ---------------------------------------------------------------------------
# Tests
#
# The e2e suite drives a real browser and is an order of magnitude slower than
# the rest, so every non-e2e target filters it out with -m 'not e2e'.
# ---------------------------------------------------------------------------

.PHONY: test test-js test-e2e

test:  ## Run tests (unit + integration, excludes e2e)
	$(PYTEST) tests/ -v -m 'not e2e'

test-js:  ## Run frontend JS tests + syntax check
	@node --check src/harbor/static/harbor-utils.js && echo "harbor-utils.js: syntax OK"
	@node tests/frontend/test-utils.js

test-e2e: e2e-setup  ## Run end-to-end browser tests (requires playwright)
	$(PYTEST) tests/e2e/ -m e2e --browser chromium

# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------

.PHONY: coverage coverage-html

coverage:  ## Run tests with coverage report (fails if below threshold in pyproject.toml, excludes e2e)
	@# pytest-cov enforces fail_under from [tool.coverage.report] and exits 1 when
	@# the threshold is not met, so no second `coverage report` pass is needed.
	$(PYTEST) tests/ -m 'not e2e' --cov=harbor --cov-report=term-missing

coverage-html:  ## Run tests and generate HTML coverage report in htmlcov/ (excludes e2e)
	$(PYTEST) tests/ -m 'not e2e' --cov=harbor --cov-report=html --cov-report=term-missing
	@echo "Open htmlcov/index.html in your browser to view the report."

# ---------------------------------------------------------------------------
# Quality
# ---------------------------------------------------------------------------

.PHONY: lint format

lint:  ## Run lint checks (ruff + mypy)
	$(PYTHON) -m ruff check src/ tests/
	@# mypy only checks src/ (progressive, not required to pass yet — informational)
	-$(PYTHON) -m mypy src/ 2>/dev/null || echo 'mypy: skipped (not installed or informational only)'

format:  ## Auto-fix lint findings and format (same pair as the pre-commit hooks)
	$(PYTHON) -m ruff check --fix src/ tests/
	$(PYTHON) -m ruff format src/ tests/

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

.PHONY: build build-check

build:  ## Build the package
	$(PYTHON) -m build

build-check: build  ## Build the package and check metadata with twine
	$(PYTHON) -m twine check dist/*

# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------

.PHONY: clean

clean:  ## Remove build artifacts and tool caches
	rm -rf build/ dist/ *.egg-info/ src/*.egg-info/
	rm -rf .pytest_cache/ .ruff_cache/ .mypy_cache/ .coverage htmlcov/
	@# Recursive so newly added subpackages and test dirs are covered too.
	find . -name __pycache__ -type d -not -path './.git/*' -prune -exec rm -rf {} +
