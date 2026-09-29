PYTHON ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

.PHONY: install run rpc-smoke test lint format type-check check

install:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/python -m pip install -e ".[dev]"

run:
	$(BIN)/python -m ethereum_wallet_intelligence

rpc-smoke:
	$(BIN)/python -m ethereum_wallet_intelligence.rpc_smoke

test:
	$(BIN)/pytest

lint:
	$(BIN)/ruff check .

format:
	$(BIN)/ruff format .

type-check:
	$(BIN)/mypy src tests

check: lint type-check test
