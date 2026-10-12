"""PostgreSQL / SQLAlchemy persistence infrastructure."""

from ethereum_wallet_intelligence.infrastructure.persistence.database import (
    create_database_engine,
    create_session_factory,
)
from ethereum_wallet_intelligence.infrastructure.persistence.mappers import (
    block_to_domain,
    block_to_model,
    checkpoint_to_domain,
    checkpoint_to_model,
    transaction_to_domain,
    transaction_to_model,
)
from ethereum_wallet_intelligence.infrastructure.persistence.models import (
    Base,
    BlockModel,
    IndexerCheckpointModel,
    TransactionModel,
)
from ethereum_wallet_intelligence.infrastructure.persistence.repositories import (
    SQLAlchemyBlockRepository,
    SQLAlchemyCheckpointRepository,
    SQLAlchemyTransactionRepository,
)
from ethereum_wallet_intelligence.infrastructure.persistence.unit_of_work import (
    SQLAlchemyIndexerUnitOfWork,
)

__all__ = [
    "Base",
    "BlockModel",
    "IndexerCheckpointModel",
    "SQLAlchemyBlockRepository",
    "SQLAlchemyCheckpointRepository",
    "SQLAlchemyIndexerUnitOfWork",
    "SQLAlchemyTransactionRepository",
    "TransactionModel",
    "block_to_domain",
    "block_to_model",
    "checkpoint_to_domain",
    "checkpoint_to_model",
    "create_database_engine",
    "create_session_factory",
    "transaction_to_domain",
    "transaction_to_model",
]
