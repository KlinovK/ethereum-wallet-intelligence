"""SQLAlchemy Unit of Work for atomic indexer persistence."""

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ethereum_wallet_intelligence.infrastructure.persistence.repositories import (
    SQLAlchemyBlockRepository,
    SQLAlchemyCheckpointRepository,
    SQLAlchemyTransactionRepository,
)


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
        except BaseException:
            await session.close()
            raise

        self._session = session
        self._blocks = SQLAlchemyBlockRepository(session)
        self._transactions = SQLAlchemyTransactionRepository(session)
        self._checkpoints = SQLAlchemyCheckpointRepository(session)
        self._committed = False
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        session = self._session
        if session is None:
            return

        try:
            if session.in_transaction():
                await session.rollback()
        finally:
            try:
                await session.close()
            finally:
                self._session = None
                self._blocks = None
                self._transactions = None
                self._checkpoints = None
                self._committed = False

    async def commit(self) -> None:
        """Commit the active transaction exactly once."""
        session = self._ensure_active()
        await session.commit()
        self._committed = True

    def _ensure_active(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("Indexer Unit of Work is not active")
        if self._committed:
            raise RuntimeError("Indexer Unit of Work has already committed")
        return self._session
