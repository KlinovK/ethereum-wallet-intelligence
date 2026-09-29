# Ethereum Wallet Intelligence

Ethereum Wallet Intelligence is a backend service that will accept Ethereum wallet addresses,
index their on-chain activity, normalize the resulting blockchain data, and expose portfolio,
transaction, token, DeFi, profit-and-loss, and wallet analytics.

## Current status

Phase 4 adds a read-only application port for Ethereum blockchain access and an async web3.py
adapter for standard JSON-RPC providers. The service currently exposes one endpoint:

```text
GET /health
```

It returns HTTP 200 with `{"status": "ok"}`. PostgreSQL is available as local development
infrastructure, but the application intentionally does not connect to it yet.

Domain address validation remains intentionally structural: valid addresses are normalized to
lowercase, but this is not EIP-55 checksum validation. The web3.py adapter derives a checksummed
form only when making provider calls; checksum-specific behavior does not leak into the domain.

## Architecture

The project is growing as a modular monolith. Clean Architecture and Ports & Adapters patterns
will be introduced where real use cases create a need for them, rather than as placeholder
layers. The dependency rule is that business and domain code must not depend on delivery or
infrastructure frameworks such as FastAPI, SQLAlchemy, PostgreSQL clients, or web3.py.

The package contains the composition root, health endpoint, environment-backed settings, and a
small domain package for the implemented Ethereum value objects and blockchain data models. There
is now a framework-independent application `BlockchainGateway` protocol, implemented by the
infrastructure `Web3BlockchainGateway`. Web3-specific types remain inside infrastructure.

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
| `ETHEREUM_RPC_URL` | unset | HTTP(S) Ethereum JSON-RPC endpoint |

The package bootstrap uses the validated host and port settings when it starts Uvicorn.
`ETHEREUM_RPC_URL` is optional for the health endpoint and normal unit tests. It is required only
when constructing the RPC adapter; credentials embedded in it are held as a masked setting and
must never be committed. Settings are loaded from the repository-root `.env` when commands run
from the project directory; process environment variables take precedence over `.env` values.

To perform a manual, read-only RPC smoke check, set `ETHEREUM_RPC_URL` in the process environment
and run:

```bash
make rpc-smoke
```

The command reads the current block number and that block, then prints the block number, block
hash, parent hash, and UTC timestamp. It never prints the provider URL and does not use private
keys or submit transactions. The smoke check is not part of the test suite.

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

`pytest-asyncio` is not included because the adapter tests execute coroutines with the standard
library's `asyncio.run`; no additional async test runtime is currently necessary.
