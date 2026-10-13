"""Application-facing ports and errors."""

from ethereum_wallet_intelligence.application.block_indexer import (
    BlockIndexer,
    BlockIndexingError,
    ChainContinuityError,
    CommitNotDurableError,
    CommitReconciliationError,
    IndexingConsistencyError,
    InvalidBlockRangeError,
    RpcRetryExhaustedError,
    RpcRetryPolicy,
)
from ethereum_wallet_intelligence.application.blockchain import (
    BlockBundle,
    BlockchainGateway,
    BlockchainProviderError,
    BlockchainResourceNotFoundError,
    InvalidBlockchainResponseError,
    PermanentBlockchainProviderError,
    TransientBlockchainProviderError,
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
from ethereum_wallet_intelligence.application.unit_of_work import (
    CommitOutcomeUnknownError,
    IndexerUnitOfWork,
    UnitOfWorkCleanupError,
)

__all__ = [
    "BlockBundle",
    "BlockIndexer",
    "BlockIndexingError",
    "BlockRepository",
    "BlockchainGateway",
    "BlockchainProviderError",
    "BlockchainResourceNotFoundError",
    "ChainContinuityError",
    "CheckpointConflictError",
    "CheckpointRepository",
    "CommitNotDurableError",
    "CommitOutcomeUnknownError",
    "CommitReconciliationError",
    "IndexerCheckpoint",
    "IndexerUnitOfWork",
    "IndexingConsistencyError",
    "InvalidBlockRangeError",
    "InvalidBlockchainResponseError",
    "PermanentBlockchainProviderError",
    "PersistenceConflictError",
    "PersistenceError",
    "RpcRetryExhaustedError",
    "RpcRetryPolicy",
    "TransactionRepository",
    "TransientBlockchainProviderError",
    "UnitOfWorkCleanupError",
]
