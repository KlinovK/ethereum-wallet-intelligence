"""Unit tests for persistence mappers."""

from datetime import UTC, datetime
from decimal import Decimal

from ethereum_wallet_intelligence.application import IndexerCheckpoint
from ethereum_wallet_intelligence.domain.ethereum import (
    Block,
    BlockHash,
    BlockNumber,
    ChainId,
    EthereumAddress,
    Transaction,
    TransactionHash,
    Wei,
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
    BlockModel,
    IndexerCheckpointModel,
    TransactionModel,
)

BLOCK_HASH = "0x" + "a1" * 32
PARENT_HASH = "0x" + "b2" * 32
TX_HASH = "0x" + "c3" * 32
SENDER = "0x" + "11" * 20
RECIPIENT = "0x" + "22" * 20
UINT256_MAX = 2**256 - 1


def test_checkpoint_mapping_roundtrip() -> None:
    checkpoint = IndexerCheckpoint(
        chain_id=ChainId.ethereum_mainnet(),
        start_block_number=BlockNumber(20_000_000),
        next_block_number=BlockNumber(20_000_123),
        expected_parent_hash=BlockHash(PARENT_HASH),
    )

    model = checkpoint_to_model(checkpoint)

    assert isinstance(model, IndexerCheckpointModel)
    assert model.id == 1
    assert model.chain_id == 1
    assert model.start_block_number == 20_000_000
    assert model.next_block_number == 20_000_123
    assert model.expected_parent_hash == PARENT_HASH
    assert checkpoint_to_domain(model) == checkpoint


def test_block_mapping_roundtrip() -> None:
    timestamp = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    block = Block(
        number=BlockNumber(20_000_000),
        hash=BlockHash(BLOCK_HASH),
        parent_hash=BlockHash(PARENT_HASH),
        timestamp=timestamp,
    )

    model = block_to_model(block)

    assert isinstance(model, BlockModel)
    assert model.number == 20_000_000
    assert model.hash == BLOCK_HASH
    assert model.parent_hash == PARENT_HASH
    assert model.timestamp == timestamp

    domain = block_to_domain(model)

    assert domain == block
    assert domain.number == BlockNumber(20_000_000)
    assert domain.hash == BlockHash(BLOCK_HASH)
    assert domain.parent_hash == BlockHash(PARENT_HASH)
    assert domain.timestamp == timestamp


def test_transaction_mapping_roundtrip_with_recipient() -> None:
    tx = Transaction(
        hash=TransactionHash(TX_HASH),
        block_number=BlockNumber(20_000_000),
        sender=EthereumAddress(SENDER),
        to=EthereumAddress(RECIPIENT),
        value=Wei(10**18),
        nonce=5,
        gas_limit=21_000,
        input_data=b"\xde\xad\xbe\xef",
    )

    model = transaction_to_model(tx)

    assert isinstance(model, TransactionModel)
    assert model.hash == TX_HASH
    assert model.block_number == 20_000_000
    assert model.sender == SENDER
    assert model.recipient == RECIPIENT
    assert model.value_wei == Decimal(10**18)
    assert model.nonce == 5
    assert model.gas_limit == 21_000
    assert model.input == b"\xde\xad\xbe\xef"

    domain = transaction_to_domain(model)

    assert domain == tx
    assert domain.to == EthereumAddress(RECIPIENT)
    assert domain.value == Wei(10**18)
    assert domain.input_data == b"\xde\xad\xbe\xef"


def test_transaction_mapping_roundtrip_contract_creation_null_recipient() -> None:
    tx = Transaction(
        hash=TransactionHash(TX_HASH),
        block_number=BlockNumber(20_000_000),
        sender=EthereumAddress(SENDER),
        to=None,
        value=Wei(0),
        nonce=0,
        gas_limit=500_000,
        input_data=b"\x60\x80\x60\x40",
    )

    model = transaction_to_model(tx)

    assert model.recipient is None
    assert model.value_wei == Decimal(0)

    domain = transaction_to_domain(model)

    assert domain == tx
    assert domain.to is None
    assert domain.value == Wei(0)


def test_transaction_mapping_exact_max_uint256_value() -> None:
    tx = Transaction(
        hash=TransactionHash(TX_HASH),
        block_number=BlockNumber(20_000_000),
        sender=EthereumAddress(SENDER),
        to=EthereumAddress(RECIPIENT),
        value=Wei(UINT256_MAX),
        nonce=1,
        gas_limit=21_000,
        input_data=b"",
    )

    model = transaction_to_model(tx)

    assert model.value_wei == Decimal(UINT256_MAX)

    domain = transaction_to_domain(model)

    assert domain.value == Wei(UINT256_MAX)
    assert domain.value.value == UINT256_MAX
