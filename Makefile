PYTHON ?= python3

.PHONY: help run dev test lint build build-check clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-12s\033[0m %s\n", $$1, $$2}'

run:    ## Run Harbor (current directory)
	PYTHONPATH=src $(PYTHON) -m harbor

dev:    ## Install in development mode with dev extras (requires pip)
	$(PYTHON) -m pip install -e ".[dev]" --break-system-packages 2>/dev/null || $(PYTHON) -m pip install -e ".[dev]"

test:   ## Run tests
	PYTHONPATH=src $(PYTHON) -m pytest tests/ -v

lint:   ## Run lint checks (ruff)
	$(PYTHON) -m ruff check src/ tests/

build:  ## Build the package
	$(PYTHON) -m build

build-check: build  ## Build the package and check metadata with twine
	$(PYTHON) -m twine check dist/*

clean:  ## Remove build artifacts
	rm -rf build/ dist/ *.egg-info/ src/*.egg-info/ .pytest_cache/ __pycache__/ src/harbor/__pycache__/