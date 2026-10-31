# The Home Assistant integration's checks. The Scrypted plugin has its own (`cd scrypted-plugin
# && npm test`).
#
#   make setup     once, or after a Python upgrade
#   make check     lint, format and tests: what CI runs
#
# Home Assistant 2026.10 needs Python 3.14. If the default python3 is older:
#
#   make setup PYTHON=$$(pyenv root)/versions/3.14.6/bin/python3

VENV   ?= .venv
PYTHON ?= python3
PY     := $(VENV)/bin/python

.DEFAULT_GOAL := help
.PHONY: help setup check lint format format-check test venv-check

help: ## List the targets
	@grep -hE '^[a-z][a-z-]*:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "} {printf "  %-14s %s\n", $$1, $$2}'

setup: ## Create the virtualenv and install the test dependencies
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -r requirements-test.txt

venv-check:
	@test -x $(PY) || { printf 'No virtualenv at $(VENV). Run: make setup\n'; exit 1; }

check: lint format-check test ## Everything CI runs

lint: venv-check ## Ruff lint
	@$(PY) -m ruff check .

format: venv-check ## Ruff format, writing the files
	@$(PY) -m ruff format .

format-check: venv-check ## Ruff format, reporting only
	@$(PY) -m ruff format --check .

test: venv-check ## The Python test suite (ARGS passes through to pytest)
	@$(PY) -m pytest tests -q $(ARGS)
