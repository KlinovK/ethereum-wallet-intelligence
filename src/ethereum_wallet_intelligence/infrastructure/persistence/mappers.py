from datetime import UTC
from decimal import Decimal

from ethereum_wallet_intelligence.domain.ethereum import (
    Block,
    BlockHash,
    BlockNumber,
    EthereumAddress,
    Transaction,
    TransactionHash,
    Wei,
)
from ethereum_wallet_intelligence.infrastructure.persistence.models import (
    BlockModel,
    TransactionModel,
)


def block_to_domain(model: BlockModel) -> Block:
    """Map a BlockModel database entity to a domain Block."""
    timestamp = model.timestamp
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return Block(
        number=BlockNumber(model.number),
        hash=BlockHash(model.hash),
        parent_hash=BlockHash(model.parent_hash),
        timestamp=timestamp,
    )


def block_to_model(domain: Block) -> BlockModel:
    """Map a domain Block to a BlockModel database entity."""
    return BlockModel(
        number=domain.number.value,
        hash=domain.hash.value,
        parent_hash=domain.parent_hash.value,
        timestamp=domain.timestamp,
    )


def transaction_to_domain(model: TransactionModel) -> Transaction:
    """Map a TransactionModel database entity to a domain Transaction."""
    return Transaction(
        hash=TransactionHash(model.hash),
        block_number=BlockNumber(model.block_number),
        sender=EthereumAddress(model.sender),
        to=EthereumAddress(model.recipient) if model.recipient is not None else None,
        value=Wei(int(model.value_wei)),
        nonce=model.nonce,
        gas_limit=model.gas_limit,
        input_data=bytes(model.input),
    )


def transaction_to_model(domain: Transaction) -> TransactionModel:
    """Map a domain Transaction to a TransactionModel database entity."""
    return TransactionModel(
        hash=domain.hash.value,
        block_number=domain.block_number.value,
        sender=domain.sender.value,
        recipient=domain.to.value if domain.to is not None else None,
        value_wei=Decimal(domain.value.value),
        nonce=domain.nonce,
        gas_limit=domain.gas_limit,
        input=domain.input_data,
    )
