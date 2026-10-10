PYTHON ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

.PHONY: install run rpc-smoke test test-unit test-db-up test-integration test-all migrate migrate-down lint format type-check check

install:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/python -m pip install -e ".[dev]"

run:
	$(BIN)/python -m ethereum_wallet_intelligence

rpc-smoke:
	$(BIN)/python -m ethereum_wallet_intelligence.rpc_smoke

test:
	$(BIN)/pytest -m "not integration"

test-unit:
	$(BIN)/pytest -m "not integration"

test-db-up:
	docker compose up -d --wait postgres-test

test-integration: test-db-up
	$(BIN)/pytest -m integration

test-all: test-db-up
	$(BIN)/pytest

migrate:
	$(BIN)/alembic upgrade head

migrate-down:
	$(BIN)/alembic downgrade -1

lint:
	$(BIN)/ruff check .

format:
	$(BIN)/ruff format .

type-check:
	$(BIN)/mypy src tests

check: lint type-check test
