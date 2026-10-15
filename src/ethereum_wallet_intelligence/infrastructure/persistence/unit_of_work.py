"""SQLAlchemy Unit of Work for atomic indexer persistence."""

import logging
from asyncio import CancelledError
from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ethereum_wallet_intelligence.application.unit_of_work import (
    CommitOutcomeUnknownError,
    UnitOfWorkCleanupError,
)
from ethereum_wallet_intelligence.infrastructure.persistence.repositories import (
    SQLAlchemyBlockRepository,
    SQLAlchemyCheckpointRepository,
    SQLAlchemyTransactionRepository,
)

_logger = logging.getLogger(__name__)


async def _dispose_session(
    session: AsyncSession,
    *,
    operation: str,
    cancellation: CancelledError | None,
) -> tuple[bool, str | None, CancelledError | None]:
    """Release a session while retaining the earliest cancellation signal."""
    cleanup_stage: str | None = None
    invalidation_stage: str | None = None
    disposal_confirmed = False

    try:
        if session.in_transaction():
            await session.rollback()
    except CancelledError as error:
        if cancellation is None:
            cancellation = error
        cleanup_stage = "rollback_cancelled"
    except Exception:
        cleanup_stage = "rollback_failed"

    if cleanup_stage is None:
        try:
            await session.close()
            disposal_confirmed = True
        except CancelledError as error:
            if cancellation is None:
                cancellation = error
            cleanup_stage = "close_cancelled"
        except Exception:
            cleanup_stage = "close_failed"

    if cleanup_stage is not None:
        _logger.warning(
            "unit_of_work_cleanup_recovery_started",
            extra={
                "event": "unit_of_work_cleanup_recovery_started",
                "operation": operation,
                "stage": cleanup_stage,
            },
        )
        try:
            await session.invalidate()
            disposal_confirmed = True
            invalidation_stage = "invalidation_succeeded"
        except CancelledError as error:
            if cancellation is None:
                cancellation = error
            invalidation_stage = "invalidation_cancelled"
        except Exception:
            invalidation_stage = "invalidation_failed"

    if not disposal_confirmed:
        safe_invalidation_stage = invalidation_stage or "invalidation_not_attempted"
        _logger.error(
            "unit_of_work_cleanup_unconfirmed",
            extra={
                "event": "unit_of_work_cleanup_unconfirmed",
                "operation": operation,
                "stage": cleanup_stage or "cleanup_unknown",
                "invalidation_stage": safe_invalidation_stage,
            },
        )
        if cancellation is not None:
            cancellation.add_note(
                "Database session cleanup could not be confirmed "
                f"(operation={operation}, stage={cleanup_stage or 'cleanup_unknown'}, "
                f"invalidation_stage={safe_invalidation_stage})"
            )
    elif cleanup_stage is not None:
        _logger.warning(
            "unit_of_work_cleanup_recovered",
            extra={
                "event": "unit_of_work_cleanup_recovered",
                "operation": operation,
                "stage": cleanup_stage,
                "invalidation_stage": invalidation_stage,
            },
        )

    return disposal_confirmed, cleanup_stage, cancellation


class SQLAlchemyIndexerUnitOfWork:
    """Own one session and transaction shared by all indexing repositories."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self._blocks: SQLAlchemyBlockRepository | None = None
        self._transactions: SQLAlchemyTransactionRepository | None = None
        self._checkpoints: SQLAlchemyCheckpointRepository | None = None
        self._committed = False

    @property
    def blocks(self) -> SQLAlchemyBlockRepository:
        """Return the block repository bound to the active transaction."""
        self._ensure_active()
        assert self._blocks is not None
        return self._blocks

    @property
    def transactions(self) -> SQLAlchemyTransactionRepository:
        """Return the transaction repository bound to the active transaction."""
        self._ensure_active()
        assert self._transactions is not None
        return self._transactions

    @property
    def checkpoints(self) -> SQLAlchemyCheckpointRepository:
        """Return the checkpoint repository bound to the active transaction."""
        self._ensure_active()
        assert self._checkpoints is not None
        return self._checkpoints

    async def __aenter__(self) -> Self:
        if self._session is not None:
            raise RuntimeError("Indexer Unit of Work is already active")

        session = self._session_factory()
        try:
            await session.begin()
        except BaseException as error:
            primary_error = error
        else:
            self._session = session
            self._blocks = SQLAlchemyBlockRepository(session)
            self._transactions = SQLAlchemyTransactionRepository(session)
            self._checkpoints = SQLAlchemyCheckpointRepository(session)
            self._committed = False
            return self

        primary_cancellation = primary_error if isinstance(primary_error, CancelledError) else None
        disposal_confirmed, _, cancellation = await _dispose_session(
            session,
            operation="enter",
            cancellation=primary_cancellation,
        )
        if cancellation is not None:
            raise cancellation
        if not disposal_confirmed:
            raise UnitOfWorkCleanupError(
                "Database session cleanup could not be confirmed; indexing stopped safely"
            )
        raise primary_error

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, traceback
        session = self._session
        if session is None:
            return

        primary_cancellation = exc_value if isinstance(exc_value, CancelledError) else None
        try:
            disposal_confirmed, cleanup_stage, cancellation = await _dispose_session(
                session,
                operation="exit",
                cancellation=primary_cancellation,
            )
        finally:
            self._session = None
            self._blocks = None
            self._transactions = None
            self._checkpoints = None
            self._committed = False

        if cancellation is not None:
            raise cancellation

        if not disposal_confirmed:
            raise UnitOfWorkCleanupError(
                "Database session cleanup could not be confirmed; indexing stopped safely"
            )

        if cleanup_stage is not None and exc_value is None:
            raise UnitOfWorkCleanupError(
                "Database session cleanup required connection invalidation"
            )

    async def commit(self) -> None:
        """Commit the active transaction exactly once."""
        session = self._ensure_active()
        translated_error: CommitOutcomeUnknownError | None = None
        try:
            await session.commit()
        except Exception:
            translated_error = CommitOutcomeUnknownError(
                "Database commit outcome is unknown and requires reconciliation"
            )
        if translated_error is not None:
            raise translated_error
        self._committed = True

    def _ensure_active(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("Indexer Unit of Work is not active")
        if self._committed:
            raise RuntimeError("Indexer Unit of Work has already committed")
        return self._session
