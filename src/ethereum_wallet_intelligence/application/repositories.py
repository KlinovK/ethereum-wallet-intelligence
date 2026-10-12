"""Application repository ports and persistence exceptions."""

from typing import Protocol

from ethereum_wallet_intelligence.application.indexing import IndexerCheckpoint
from ethereum_wallet_intelligence.domain.ethereum import (
    Block,
    BlockNumber,
    Transaction,
    TransactionHash,
)


class PersistenceError(Exception):
    """Base exception for persistence-related failures."""


class PersistenceConflictError(PersistenceError):
    """Raised when attempting to persist conflicting immutable blockchain data."""


class CheckpointConflictError(PersistenceConflictError):
    """Raised when a checkpoint write is stale or violates indexing continuity."""


class BlockRepository(Protocol):
    """Repository port for Ethereum block persistence."""

    async def get_by_number(self, block_number: BlockNumber) -> Block | None: ...

    async def has_any(self) -> bool: ...

    async def save(self, block: Block) -> None: ...

    async def delete_after(self, block_number: BlockNumber) -> None: ...


class TransactionRepository(Protocol):
    """Repository port for Ethereum transaction persistence."""

    async def get_by_hash(self, transaction_hash: TransactionHash) -> Transaction | None: ...

    async def has_any(self) -> bool: ...

    async def save(self, transaction: Transaction) -> None: ...


class CheckpointRepository(Protocol):
    """Repository port for the singleton indexing checkpoint."""

    async def get_for_update(self) -> IndexerCheckpoint | None: ...

    async def create(self, checkpoint: IndexerCheckpoint) -> None: ...

    async def advance(
        self,
        expected: IndexerCheckpoint,
        block: Block,
    ) -> IndexerCheckpoint: ...

    async def rewind(
        self,
        expected: IndexerCheckpoint,
        ancestor: Block,
    ) -> IndexerCheckpoint: ...
