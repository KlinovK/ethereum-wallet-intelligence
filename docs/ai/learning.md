# Learning Guidance

The developer is an experienced Senior iOS engineer expanding into Python backend, fullstack, Web3, and AI engineering. Learning support should increase understanding without turning routine implementation into a tutorial.

## Learning goals

### Python Backend Engineering

Build a practical mental model of Python typing, immutable data modeling, exceptions, `asyncio` cancellation, FastAPI composition, configuration, SQLAlchemy sessions, PostgreSQL transactions, migrations, and operational failure handling.

### Fullstack Engineering

Develop understanding of HTTP contracts, validation boundaries, error semantics, API evolution, and the relationship between a backend's domain, persistence, delivery layer, and future clients. Do not imply that a frontend exists before one is implemented.

### AI Engineering workflows

Practice precise task scoping, separation of design/audit/implementation modes, evidence-based code review, deterministic verification, and safe automation. Treat generated code and confident explanations as hypotheses until checked against the repository and executable tests.

### Web3 Engineering

Understand exact integer quantities, chain identity, block and transaction invariants, RPC trust boundaries, idempotent indexing, finality, reorganizations, and the difference between reading canonical data and submitting state-changing transactions.

### Senior iOS skill development

Transfer existing strengths in protocol-oriented design, value semantics, async/await, cancellation, dependency injection, and testability. Extend them with server-side transaction ownership, multi-process concurrency, database constraints, deployment concerns, and distributed-system ambiguity.

## How to explain

- Explain one core concept when it first becomes relevant, then build on it rather than repeating a full primer.
- Lead with the conceptual model and observable consequences; use line-by-line walkthroughs only when debugging a subtle execution path.
- Use Swift comparisons when useful: Python protocols as a structural counterpart to Swift protocols, frozen dataclasses as value-like models, and `asyncio` cancellation as similar in intent but not identical to Swift task cancellation.
- State where an analogy breaks down. SQL transactions, process concurrency, Python exception context, and runtime typing should not be forced into an iOS metaphor.
- Explain important alternatives and trade-offs: correctness versus throughput, optimistic checks versus locks, convenience versus dependency direction, and retries versus ambiguity.
- Distinguish **Fact** (verified behavior or documented semantics), **Assumption** (needed but not yet verified), and **Recommendation** (a proposed choice with rationale).
- Keep explanations concise and practical. Prefer a small example, invariant, or failure scenario over abstract terminology.

## Division of work

Codex should automate implementation, routine refactoring, test execution, repository inspection, and mechanical verification within the user's authorization. ChatGPT-style collaboration should emphasize architecture, planning, learning, adversarial review, and interpretation of results. Neither role may treat prior reports as evidence when the code or current test output can be inspected directly.

Implementation reports should briefly explain non-obvious concepts and decisions that advance the learning goals. Do not add tutorial files, comments, abstractions, or dependencies solely for educational value; production quality and the current product scope remain controlling constraints.
