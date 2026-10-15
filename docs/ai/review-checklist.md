# Engineering Review Checklist

Use this checklist against the actual diff and execution path. Check only applicable items, but explain any high-risk item that cannot be verified.

## Scope and correctness

- [ ] The diff contains only requested changes and preserves pre-existing uncommitted work.
- [ ] Each public operation has explicit preconditions, postconditions, and invalid-state behavior.
- [ ] Domain invariants are enforced where values are created, not only at API or database boundaries.
- [ ] Identity comparisons use all required immutable fields; a matching height or count is not treated as sufficient identity.
- [ ] Empty, zero, nullable, maximum-size, and duplicate cases have intentional behavior.

## Boundaries and off-by-one behavior

- [ ] Inclusive and exclusive range endpoints match the documented contract.
- [ ] Maximum sizes are tested at the limit and one past it.
- [ ] Start boundaries, genesis behavior, and already-complete ranges are handled explicitly.
- [ ] “At most three” means attempts 1–3 are permitted and attempt 4 fails before discovery or mutation.
- [ ] Rewind calculations count deleted descendants, not merely distance between two labels.

## Bounded loops, retries, and recovery

- [ ] Every loop and retry path has an effective bound or a clear finite progress invariant.
- [ ] A single invocation-wide budget is shared by startup, completed-range, mid-index, and post-recovery paths.
- [ ] Helper calls cannot recreate a budget and revive an exhausted recovery loop.
- [ ] Transient RPC failures alone are retried; malformed data, divergence, application errors, and database writes fail without retry.
- [ ] Timeout, backoff, jitter, attempt count, and any lower-level library retries are bounded together.
- [ ] A changing provider cannot force an infinite discover–rewind–reindex cycle.

## Concurrency and stale state

- [ ] RPC reads occur before database locks are acquired.
- [ ] The checkpoint is locked and re-read before canonical mutation.
- [ ] Expected state is compared exactly, including chain, start, next height, and expected parent hash.
- [ ] A concurrent advance, rewind, or ancestor replacement makes a stale plan fail closed.
- [ ] Concurrent singleton initialization cannot silently overwrite another writer.
- [ ] The test distinguishes real row locking from a mock that merely returns expected data.

## Database atomicity and commit uncertainty

- [ ] One Unit of Work owns the session and explicit commit; repositories never commit.
- [ ] Related block, transaction, and checkpoint changes use one PostgreSQL transaction.
- [ ] Descendant block deletion and checkpoint rewind commit atomically, with transaction deletion enforced by the real cascade.
- [ ] Pre-commit failure leaves the exact original state and no partial durable rows.
- [ ] Durable commit with lost acknowledgement is reconciled through a fresh session after safe disposal of the old one.
- [ ] Intended-state reconciliation checks exact checkpoint, block identity, highest retained block, expected transaction hashes/count/content, and absence of unexpected rows.
- [ ] Non-durable reconciliation proves the complete original snapshot, not merely an unchanged checkpoint.
- [ ] Original transaction hashes are checked globally; empty deleted heights do not prove that a hash did not survive under another block.
- [ ] Partial or concurrent state is never mistaken for either success or safe non-durability.
- [ ] No database write is automatically replayed after an ambiguous outcome.

## Cancellation and resource cleanup

- [ ] Cancellation propagates through RPC calls, backoff, persistence, commit, reconciliation, and CLI boundaries.
- [ ] Rollback, invalidation, and close paths are exercised independently and in combination.
- [ ] Cleanup runs on exceptions and cancellation without swallowing the triggering `BaseException`.
- [ ] A cleanup cancellation is not replaced by a generic database error from a later cleanup step.
- [ ] Cleanup failure blocks reconciliation when safe connection disposal cannot be proven.
- [ ] Sessions and connections cannot be reused after ambiguous commit or failed cleanup.

## Exception and secret safety

- [ ] Application errors contain stable categories and safe metadata, not raw infrastructure messages.
- [ ] Infrastructure exceptions are neither chained nor retained as application-error attributes at credential-bearing boundaries.
- [ ] Logs and CLI output omit RPC URLs, database URLs, headers, credentials, raw payloads, and transaction input.
- [ ] Complete rendered tracebacks from injected credential-bearing failures contain no secret.
- [ ] Cancellation remains cancellation rather than being translated into a provider or database failure.

## Blockchain and reorganization behavior

- [ ] Chain ID, returned height, block hash, parent hash, transaction membership, and unique hashes are validated.
- [ ] Canonical tip verification runs at startup and for an already-completed requested range.
- [ ] Local and provider ancestry are each parent-linked and missing history fails closed.
- [ ] The common ancestor is exact, within the rewind limit, and never before the original checkpoint start.
- [ ] The candidate ancestor and descendants are revalidated before mutation without making RPC calls under the database lock.
- [ ] No verified ancestor means zero database mutation.
- [ ] Repeated provider changes consume the same recovery budget; the mismatched fetched bundle is discarded.
- [ ] Commit reconciliation verifies deleted blocks and transactions as well as the rewound checkpoint.

## Test fidelity

- [ ] Unit tests are deterministic and isolate only the intended collaborator.
- [ ] Integration tests use explicit safe test configuration and cannot fall back to the application database.
- [ ] Destructive SQL is preceded by target validation.
- [ ] Commit fault injection occurs at the real `AsyncSession.commit()` boundary while production Unit of Work handling remains active.
- [ ] Both “commit persisted, acknowledgement lost” and “failed before durability” are reproduced.
- [ ] Real PostgreSQL verifies locks, constraints, rollback, cascade, and fresh-session visibility.
- [ ] Assertions prove no extra mutation occurred after a limit or failure, rather than checking only the raised error.
- [ ] Test doubles do not duplicate the production algorithm so closely that both can share the same defect.

## Maintainability and release evidence

- [ ] Names describe domain intent and transaction ownership.
- [ ] New abstractions have a current caller and remove real coupling or duplication.
- [ ] Comments explain non-obvious invariants or trade-offs, not syntax.
- [ ] Public behavior and operator recovery guidance are documented where needed.
- [ ] Ruff, formatting, strict mypy, relevant pytest suites, `git diff --check`, and dependency checks have explicit results.
- [ ] Remaining risks and skipped checks are visible in the final report.
