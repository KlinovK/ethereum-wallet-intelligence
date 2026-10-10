"""Infrastructure adapters for external systems."""

from ethereum_wallet_intelligence.infrastructure.persistence import (
    Base,
    BlockModel,
    SQLAlchemyBlockRepository,
    SQLAlchemyTransactionRepository,
    TransactionModel,
    create_database_engine,
    create_session_factory,
)
from ethereum_wallet_intelligence.infrastructure.web3_gateway import Web3BlockchainGateway

__all__ = [
    "Base",
    "BlockModel",
    "SQLAlchemyBlockRepository",
    "SQLAlchemyTransactionRepository",
    "TransactionModel",
    "Web3BlockchainGateway",
    "create_database_engine",
    "create_session_factory",
]
