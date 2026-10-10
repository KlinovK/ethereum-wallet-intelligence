"""Application repository ports and persistence exceptions."""

from typing import Protocol

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


class BlockRepository(Protocol):
    """Repository port for Ethereum block persistence."""

    async def get_by_number(self, block_number: BlockNumber) -> Block | None: ...

    async def save(self, block: Block) -> None: ...


class TransactionRepository(Protocol):
    """Repository port for Ethereum transaction persistence."""

    async def get_by_hash(self, transaction_hash: TransactionHash) -> Transaction | None: ...

    async def save(self, transaction: Transaction) -> None: ...
