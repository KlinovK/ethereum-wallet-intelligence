# Repository Instructions

## Purpose and priorities

Ethereum Wallet Intelligence is a production-oriented backend for indexing Ethereum data and building wallet analytics. Make decisions in this order: correctness, security, reliability, maintainability, testability, learning value, then development speed.

## Architecture

- Preserve Clean Architecture / Ports & Adapters and inward dependency direction.
- Keep domain code framework-independent. Application use cases and ports may depend on domain types, never infrastructure implementations.
- Keep FastAPI, Pydantic settings, SQLAlchemy, PostgreSQL, Alembic, and web3.py in their appropriate outer layers.
- Implement only the requested scope. Do not add speculative abstractions or future features.

## Mandatory safety

- Read the task constraints, `git status`, and relevant diffs before acting. Preserve user and agent changes that are already present.
- A task marked **READ-ONLY** permits inspection and reporting only: do not edit files or mutate external state.
- Do not access a live Ethereum RPC endpoint or the application database without explicit user approval. PostgreSQL tests may use only the explicitly configured, fail-closed disposable test database.
- Never reveal secrets, RPC URLs, database URLs, credentials, or private keys in code, commands, logs, errors, or reports. Never submit a blockchain transaction unless explicitly authorized.
- Do not delete database volumes, reset repositories, or perform destructive cleanup unless the user explicitly requests it and the exact target has been verified.
- Do not commit or push unless the user explicitly authorizes that action.

## Working references

- Enforceable engineering rules: [`docs/ai/rules.md`](docs/ai/rules.md)
- Development workflow and reporting: [`docs/ai/workflow.md`](docs/ai/workflow.md)
- Review checklist: [`docs/ai/review-checklist.md`](docs/ai/review-checklist.md)
- Learning guidance: [`docs/ai/learning.md`](docs/ai/learning.md)

Use the repository `Makefile` and `README.md` as the source of truth for current commands. Task-specific instructions override the general workflow when they are stricter.
