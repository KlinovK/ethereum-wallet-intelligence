# AI Development Workflow

The normal sequence is **Design → Learn → Implement → Review → Verify → Commit**. It is iterative: a failed review or verification returns to implementation, followed by a new review and verification pass. Scale the ceremony to risk; trivial documentation and mechanical edits do not require a separate formal design phase.

## Task modes

Before choosing a stage, classify the request:

- **READ-ONLY**: inspect and report. Do not edit files, run migrations, access live RPC, mutate databases, or perform other state-changing actions. Report findings from the actual implementation with evidence.
- **Implementation**: the requested files and behavior may be changed within scope. This does not authorize unrelated cleanup, live external access, commits, or pushes.

Task-specific constraints take precedence whenever they are stricter.

## 1. Design

**Entry:** the objective, current repository state, and constraints have been inspected; material behavior or boundaries need a decision.

Define scope, invariants, dependency direction, transaction ownership, failure behavior, security constraints, and a verification strategy. Compare meaningful alternatives and state their trade-offs. Reuse current architecture unless change is necessary.

**Exit:** the intended behavior, exclusions, risks, affected boundaries, and tests are clear enough to implement without guessing. For a design-only task, stop here and deliver the proposal without editing.

## 2. Learn

**Entry:** the work introduces a concept, library behavior, or trade-off that merits explanation for the developer.

Explain the concept and why it matters before implementation details. Use the guidance in [learning.md](learning.md), authoritative sources when external facts need verification, and concise Swift comparisons where they clarify rather than distort.

**Exit:** facts, assumptions, recommendations, and the selected trade-off are distinguishable. This stage may be brief or combined with Design for familiar or mechanical work.

## 3. Implement

**Entry:** the user has requested a change; scope and safety constraints are known; `git status` and relevant diffs have been inspected.

Preserve existing uncommitted work. Implement the smallest complete change, keep dependencies inward, and add risk-proportionate tests. Do not expand authority: an implementation request does not permit live RPC, application database access, destructive cleanup, commit, or push unless stated explicitly.

**Exit:** requested behavior and tests are complete, no known out-of-scope edits were introduced, and the change is ready for adversarial review.

## 4. Review

**Entry:** an implementation or document change is complete enough to assess as a whole.

Review the actual diff and production execution paths using [review-checklist.md](review-checklist.md). Do not infer correctness from test names. Classify each actionable finding:

- **P0 — Critical:** immediate risk of irreversible data loss, credential compromise, unauthorized external mutation, or fundamentally unsafe behavior.
- **P1 — High:** release-blocking correctness, security, concurrency, or reliability defect with a credible production failure.
- **P2 — Medium:** meaningful defect or coverage gap that can produce incorrect behavior under realistic conditions.
- **P3 — Low:** localized maintainability issue, low-risk edge case, or useful test/documentation improvement.

P0 and P1 findings must return to Implement. P2 findings should be fixed before commit unless the user explicitly accepts the residual risk. P3 findings may be deferred only when disclosed and when no requirement is violated. A READ-ONLY audit must not fix findings; report verdict, severity, exact file/line evidence, failure scenario, missing tests, and minimal remedy.

**Exit:** there are no unaccepted P0/P1 findings, and every P2/P3 has been fixed or explicitly recorded with its risk.

## 5. Verify

**Entry:** review has no unresolved release-blocking finding.

Run checks relevant to the change using current repository commands:

```bash
make test
make test-integration
make test-all
make lint
.venv/bin/ruff format --check .
make type-check
git diff --check
.venv/bin/pip check
```

Do not run every command blindly. PostgreSQL checks require the explicitly configured isolated test database and its safety guard. A documentation-only change normally requires link/path validation, scope inspection, secret scanning, Markdown inspection, `git status`, and `git diff --check`; run code suites only when the task or affected behavior requires them. Treat skipped checks as explicit report items, not silent successes.

**Exit:** every required check passes; failures have been fixed and re-run; the final diff contains only intended files; no secrets or credentials were added.

## 6. Commit

**Entry:** verification is green, review findings are resolved or accepted, and the user has explicitly authorized a commit.

Review the staged scope and create a focused commit. A request to implement or verify is not authorization to commit. A commit is not authorization to push; pushing requires separate explicit permission unless the user clearly requested both.

**Exit:** the authorized commit or push is complete and its identifier/result is reported. Without authorization, stop with the working tree uncommitted and state readiness.

## Required final report

Adapt the headings to the task, but always report:

1. Outcome and files changed or inspected.
2. Important design or implementation decisions.
3. Tests and verification commands with results, including anything not run.
4. Assumptions, remaining risks, and deliberately deferred work.
5. Commit status and readiness.

For a review-only task, replace implementation details with the verdict, findings ordered by severity, exact references, missing tests, minimal fixes, and commit readiness.
