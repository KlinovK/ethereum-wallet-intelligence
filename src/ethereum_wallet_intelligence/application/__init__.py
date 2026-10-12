"""Application-facing ports and errors."""

from ethereum_wallet_intelligence.application.block_indexer import (
    BlockIndexer,
    BlockIndexingError,
    InvalidBlockRangeError,
)
from ethereum_wallet_intelligence.application.blockchain import (
    BlockBundle,
    BlockchainGateway,
    BlockchainProviderError,
    BlockchainResourceNotFoundError,
)
from ethereum_wallet_intelligence.application.indexing import IndexerCheckpoint
from ethereum_wallet_intelligence.application.repositories import (
    BlockRepository,
    CheckpointConflictError,
    CheckpointRepository,
    PersistenceConflictError,
    PersistenceError,
    TransactionRepository,
)
from ethereum_wallet_intelligence.application.unit_of_work import IndexerUnitOfWork

__all__ = [
    "BlockBundle",
    "BlockIndexer",
    "BlockIndexingError",
    "BlockRepository",
    "BlockchainGateway",
    "BlockchainProviderError",
    "BlockchainResourceNotFoundError",
    "CheckpointConflictError",
    "CheckpointRepository",
    "IndexerCheckpoint",
    "IndexerUnitOfWork",
    "InvalidBlockRangeError",
    "PersistenceConflictError",
    "PersistenceError",
    "TransactionRepository",
]
