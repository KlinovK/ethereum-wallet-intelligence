# Ethereum Wallet Intelligence

Ethereum Wallet Intelligence is a backend service that will accept Ethereum wallet addresses,
index their on-chain activity, normalize the resulting blockchain data, and expose portfolio,
transaction, token, DeFi, profit-and-loss, and wallet analytics.

## Current status

Phase 2 adds the first framework-independent Ethereum domain primitives: `EthereumAddress`,
`Wei`, and `ChainId`. The service currently exposes one endpoint:

```text
GET /health
```

It returns HTTP 200 with `{"status": "ok"}`. PostgreSQL is available as local development
infrastructure, but the application intentionally does not connect to it yet.

Address validation is intentionally structural at this stage. Valid addresses are normalized to
lowercase, but this is not EIP-55 checksum validation. Checksum-aware behavior is deferred until
an Ethereum adapter introduces a concrete need and an appropriate Ethereum-specific dependency.

## Architecture

The project is growing as a modular monolith. Clean Architecture and Ports & Adapters patterns
will be introduced where real use cases create a need for them, rather than as placeholder
layers. The dependency rule is that business and domain code must not depend on delivery or
infrastructure frameworks such as FastAPI, SQLAlchemy, PostgreSQL clients, or web3.py.

The package contains the composition root, health endpoint, environment-backed settings, and a
small domain package for the implemented Ethereum value objects. There are no empty application
or infrastructure layers.

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

The package bootstrap uses the validated host and port settings when it starts Uvicorn.

Run the application and PostgreSQL with containers:

```bash
cp .env.example .env
docker compose up --build
```

The API is exposed at <http://127.0.0.1:8000> by default. The Compose `APP_PORT` value can change
the host-side port. PostgreSQL data is stored in a named Docker volume.

## Quality commands

```bash
make test        # Run the test suite
make lint        # Run Ruff lint checks
make format      # Format Python files with Ruff
make type-check  # Run mypy in strict mode
make check       # Run lint, type checking, and tests
```

`pytest-asyncio` is not included because the current API behavior is tested synchronously through
FastAPI's test client. It can be added when the project gains directly asynchronous behavior that
needs async tests.
