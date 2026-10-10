# Ethereum Wallet Intelligence

Ethereum Wallet Intelligence is a backend service that will accept Ethereum wallet addresses,
index their on-chain activity, normalize the resulting blockchain data, and expose portfolio,
transaction, token, DeFi, profit-and-loss, and wallet analytics.

## Current status

Phase 5 adds production-oriented asynchronous PostgreSQL persistence for Ethereum blocks and
transactions, using SQLAlchemy 2.x with asyncpg and Alembic migrations.

The service continues to expose the health endpoint:

```text
GET /health
```

It returns HTTP 200 with `{"status": "ok"}`. `DATABASE_URL` is optional for the health endpoint and
unit tests; it is required only when configuring persistence infrastructure or applying migrations.

## Architecture

The project is growing as a modular monolith following Clean Architecture / Ports & Adapters:
- **Domain Layer** (`domain/`): Pure Python value objects and models (`Block`, `Transaction`, `Wei`, `EthereumAddress`, etc.). No framework dependencies.
- **Application Layer** (`application/`): Framework-independent repository ports (`BlockRepository`, `TransactionRepository`) and gateway protocols (`BlockchainGateway`), along with domain/application exceptions (`PersistenceError`, `PersistenceConflictError`, `BlockchainProviderError`).
- **Infrastructure Layer** (`infrastructure/`):
  - Async Web3.py adapter (`Web3BlockchainGateway`) for Ethereum JSON-RPC.
  - Async SQLAlchemy 2.x repository adapters (`SQLAlchemyBlockRepository`, `SQLAlchemyTransactionRepository`) and database session helpers.
  - Explicit mappers (`mappers.py`) translating between SQLAlchemy database rows and domain models.
- **Delivery / Config** (`main.py`, `config.py`): FastAPI composition root and pydantic-settings configuration.

### Persistence and Transaction Semantics

- **Unit-of-Work and Session Ownership**: Repositories are passed an `AsyncSession`. Repository methods execute operations within the caller-provided transaction context and flush constraints where necessary, but never commit caller-owned transactions. The caller (use case / unit of work) retains full control over commit and rollback boundaries.
- **Idempotency**: Saving the exact same block or transaction multiple times is idempotent and produces a safe no-op.
- **Conflict Handling**: If an entity with the same primary identifier (block number or transaction hash) already exists with conflicting immutable blockchain data, the repository raises `PersistenceConflictError` rather than silently overwriting it.
- **Exact uint256 Range**: Ethereum amounts in wei can reach \(2^{256} - 1\) (78 decimal digits), which exceeds PostgreSQL `BIGINT` (\(2^{63} - 1\)). Wei amounts are stored in PostgreSQL using exact `NUMERIC(78, 0)` columns without floating-point precision loss.

## Requirements

- Python 3.13
- Docker and Docker Compose (optional for containerized development)

## Local development

Create a virtual environment and install the application with development tools:

```bash
make install
```

Run the API:

```bash
make run
```

Then visit <http://127.0.0.1:8000/health>. Runtime settings are read from environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `EWI_APP_NAME` | `Ethereum Wallet Intelligence` | OpenAPI application title |
| `EWI_APP_HOST` | `127.0.0.1` | Documented application host setting |
| `EWI_APP_PORT` | `8000` | Documented application port setting |
| `DATABASE_URL` | unset | PostgreSQL asyncpg connection URL |
| `TEST_DATABASE_URL` | unset | Dedicated disposable PostgreSQL integration-test URL |
| `ETHEREUM_RPC_URL` | unset | HTTP(S) Ethereum JSON-RPC endpoint |

The package bootstrap uses the validated host and port settings when it starts Uvicorn.
`DATABASE_URL` and `ETHEREUM_RPC_URL` are optional for the health endpoint and normal unit tests.
Credentials embedded in URLs are held as masked settings and never exposed in logs or validation
errors. Settings are loaded from the repository-root `.env` when commands run from the project
directory; process environment variables take precedence over `.env` values.

### Database Migrations

Apply database migrations using Alembic:

```bash
make migrate
```

To rollback the most recent migration:

```bash
make migrate-down
```

### Manual RPC Smoke Check

To perform a manual, read-only RPC smoke check, set `ETHEREUM_RPC_URL` in the process environment
and run:

```bash
make rpc-smoke
```

### Running with Docker Compose

Run the application and PostgreSQL with containers:

```bash
cp .env.example .env
docker compose up --build
```

The API is exposed at <http://127.0.0.1:8000> by default. PostgreSQL data is stored in a named Docker volume.

## Quality commands

```bash
make test             # Run unit tests (excluding external integration tests)
make test-db-up       # Start the disposable PostgreSQL test service
make test-integration # Start it and run PostgreSQL integration tests
make test-all         # Run all tests
make lint             # Run Ruff lint checks
make format           # Format Python files with Ruff
make type-check       # Run mypy in strict mode
make check            # Run lint, type checking, and unit tests
```

PostgreSQL integration tests require an explicit `TEST_DATABASE_URL` targeting the Compose
`postgres-test` service on port 5433 and database `ethereum_wallet_intelligence_test`. They never
fall back to `DATABASE_URL`. The test service stores its data in temporary memory, and the test
suite validates the target before migrations or table truncation. Copying `.env.example` provides
the local development configuration; never point `TEST_DATABASE_URL` at a database containing
application data.
