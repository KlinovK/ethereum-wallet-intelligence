"""Application Unit of Work port for atomic indexing persistence."""

from types import TracebackType
from typing import Protocol, Self

from ethereum_wallet_intelligence.application.repositories import (
    BlockRepository,
    CheckpointRepository,
    PersistenceError,
    TransactionRepository,
)


class CommitOutcomeUnknownError(RuntimeError):
    """Raised when a commit may have succeeded but its acknowledgement was lost."""


class UnitOfWorkCleanupError(PersistenceError):
    """Raised when a Unit of Work cannot confirm safe session disposal."""


class IndexerUnitOfWork(Protocol):
    """Atomic persistence boundary for one indexing operation."""

    @property
    def blocks(self) -> BlockRepository: ...

    @property
    def transactions(self) -> TransactionRepository: ...

    @property
    def checkpoints(self) -> CheckpointRepository: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...
