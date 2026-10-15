# Engineering Rules

These rules apply to implementation and review work in this repository. They define constraints; the execution process is documented in [workflow.md](workflow.md).

## Clean Architecture

- Dependencies point inward: infrastructure and delivery may depend on application and domain; application may depend on domain; domain must not depend on outer layers.
- Domain code must not import FastAPI, Pydantic, SQLAlchemy, PostgreSQL drivers, Alembic, web3.py, or RPC response types.
- Application protocols expose domain or application types, not framework sessions, ORM models, provider objects, or wire payloads.
- Infrastructure adapters translate at their boundary and must not leak infrastructure-specific exceptions or types inward.
- Composition roots construct concrete adapters. Do not hide dependency construction in domain or application modules.
- Add an abstraction only for a current use case or boundary. Do not create empty layers, generic repositories, or placeholders for anticipated features.

## Python, typing, and asynchronous behavior

- Target Python 3.13 and keep all public interfaces explicitly typed. Code must pass the repository's strict mypy configuration.
- Prefer immutable `dataclass(frozen=True, slots=True)` models for value-like data when it matches the semantics.
- Use `int` and `Decimal` for blockchain and financial quantities. Never use `float` where exactness matters.
- Use async I/O for network and database operations; do not run blocking work on the event loop.
- Avoid global mutable state. Inject time, sleeping, randomness, and infrastructure collaborators when deterministic behavior is required.
- Do not catch `BaseException` for ordinary error translation. If cleanup must observe cancellation, perform the required cleanup and re-raise the original cancellation.

## Domain modeling

- Enforce invariants at construction so invalid value objects and models cannot be created.
- Keep models focused on data the current product behavior needs; do not mirror every Ethereum RPC field.
- Normalize values only when the normalization is semantically honest. Structural lowercase address validation must not be described as EIP-55 checksum validation.
- Distinguish concepts that have different invariants, such as transaction gas limit and receipt gas used, even when both are integers.
- Preserve exact identity fields during comparison and reconciliation: height alone is not block identity, and transaction count alone is not transaction identity.

## PostgreSQL and transaction ownership

- The application Unit of Work owns the transaction and session lifecycle. Repositories may query, add, delete, and flush; they must never commit.
- Make each block transition atomic: block, transactions, and checkpoint either become durable together or not at all.
- Require explicit commit. Exiting without a successful commit must roll back; every path must release or invalidate the session/connection.
- Lock the checkpoint before validating expected state or mutating canonical data. Use expected-state comparison to reject stale writers; never silently overwrite them.
- Never hold a database lock while making an RPC request or sleeping for backoff.
- Do not automatically replay database writes after an error. A commit exception may mean durability is unknown.
- Reconcile an ambiguous commit only after the old session is safely disposed. Use a fresh Unit of Work and verify the exact intended durable state or the exact original non-durable state; fail closed on partial or ambiguous state.
- Keep schema changes in Alembic migrations and enforce important invariants with PostgreSQL constraints. Verify cascades and constraints against real PostgreSQL, not only mocks.

## Blockchain correctness

- Validate chain identity before persistence and preserve it across checkpoint changes.
- Validate requested and returned block heights, block identity, transaction membership, unique transaction hashes, and parent-hash continuity.
- Index only explicitly bounded ranges. Never infer an instruction to start at genesis or continue indefinitely.
- Treat the checkpoint as the next required height plus the expected parent hash. Do not skip heights or silently reset a checkpoint.
- Verify canonical state even when a requested range is already complete.
- Reorganization recovery must find a verified common ancestor within configured depth and the original start boundary. Revalidate a recovery plan under the checkpoint lock, delete descendants and rewind atomically, and fail closed if ancestry cannot be proven.
- Keep recovery cycles and rewind depth bounded. A shared invocation budget must not be recreated inside helper calls.
- Never submit transactions, manage private keys, or mutate blockchain state unless a task explicitly introduces and authorizes that behavior.

## RPC reliability

- Apply a finite request timeout at the actual provider transport.
- Retry only classified transient read failures, such as timeouts, connection failures, HTTP 429, and designated transient 5xx responses.
- Do not retry malformed payloads, violated domain invariants, chain divergence, arbitrary application errors, or database writes.
- Keep attempt count, exponential backoff, and jitter bounded. Disable or account for lower-level provider retries so total behavior remains bounded.
- Make sleep and jitter injectable in retry tests. Cancellation during an RPC call or backoff must propagate immediately.

## Cancellation, cleanup, and exception safety

- Cancellation must never be translated into an RPC, database, or consistency failure.
- On failure, attempt all safe cleanup needed to prevent connection reuse. Do not silently discard rollback, invalidation, or close failures.
- Do not continue into reconciliation or further writes when safe disposal of the old database connection cannot be established.
- Translate infrastructure failures into stable application errors using safe metadata such as operation, category, or HTTP status.
- Do not include raw provider/database exception messages in application errors or attach raw exceptions as attributes.
- At credential-bearing boundaries, suppress implicit and explicit exception chaining so standard tracebacks cannot expose secrets. Treat hidden `__context__` as sensitive because custom traceback renderers may inspect it.

## Secrets and credentials

- Obtain configuration through the existing settings mechanism and environment variables. Keep local `.env` files ignored.
- Never hardcode or print credentials, endpoints, complete connection strings, private keys, or authentication headers.
- Log safe event names and identifiers only. Do not log full RPC payloads or transaction input unless a specific, reviewed requirement needs them.
- Tests for sanitization must render complete standard tracebacks with credential-bearing fake exceptions and assert the secret is absent.

## Test isolation and fidelity

- Unit tests must be deterministic and require no network, Docker, live RPC, or application database.
- PostgreSQL integration tests require an explicit `TEST_DATABASE_URL`. They must never fall back to `DATABASE_URL` and must reject the application target and unsafe database names before destructive SQL.
- Apply migrations only to the isolated test database for integration verification. Never truncate application tables or delete Docker volumes as test setup.
- Exercise production paths at critical boundaries. Fault injection around `AsyncSession.commit()` must keep the production Unit of Work and its exception handling in the call path.
- Test both sides of uncertain outcomes: durable-but-unacknowledged and failed-before-durability. Verify exact block, checkpoint, transaction, locking, and cascade behavior.
- Mocks may isolate a unit but must not be presented as evidence for real PostgreSQL atomicity, row locking, disposal, or `ON DELETE CASCADE`.

## Code quality

- Make the smallest coherent change that satisfies the task and preserves existing behavior.
- Inspect existing conventions before adding files or dependencies. Do not redesign adjacent code without a demonstrated need.
- Add or update tests for observable behavior and failure paths in proportion to risk.
- Keep comments focused on non-obvious invariants and rationale; prefer clear names and small functions over narration.
- Pass all task-relevant tests and the configured Ruff, formatting, mypy, whitespace, and dependency checks before declaring implementation ready.
