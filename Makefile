.PHONY: help venv install-dev test test-v

PYTHON ?= python3
VENV := .venv
VENV_PY := $(VENV)/bin/python
VENV_PIP := $(VENV)/bin/pip
PYTEST := $(VENV)/bin/pytest

help:
	@echo "Targets:"
	@echo "  make venv         Create local virtual environment"
	@echo "  make install-dev  Install development dependencies"
	@echo "  make test         Run pytest"
	@echo "  make test-v       Run pytest (verbose)"

venv:
	$(PYTHON) -m venv $(VENV)

install-dev: venv
	$(VENV_PIP) install -r requirements-dev.txt

test: install-dev
	$(PYTEST)

test-v: install-dev
	$(PYTEST) -vv
