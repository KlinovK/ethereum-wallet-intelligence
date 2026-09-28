"""Tests for Ethereum blockchain data models."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone

import pytest

from ethereum_wallet_intelligence.domain.ethereum import (
    Block,
    BlockHash,
    BlockNumber,
    DomainValidationError,
    EthereumAddress,
    Transaction,
    TransactionHash,
    TransactionReceipt,
    Wei,
)

BLOCK_NUMBER = BlockNumber(20_000_000)
BLOCK_HASH = BlockHash(f"0x{'ab' * 32}")
PARENT_HASH = BlockHash(f"0x{'cd' * 32}")
TRANSACTION_HASH = TransactionHash(f"0x{'ef' * 32}")
SENDER = EthereumAddress("0x1111111111111111111111111111111111111111")
RECIPIENT = EthereumAddress("0x2222222222222222222222222222222222222222")
TRANSFER_VALUE = Wei(10**18)


def make_transaction(
    *,
    recipient: EthereumAddress | None = RECIPIENT,
    value: Wei = TRANSFER_VALUE,
    nonce: int = 7,
    gas_limit: int = 21_000,
    input_data: bytes = b"",
) -> Transaction:
    return Transaction(
        hash=TRANSACTION_HASH,
        block_number=BLOCK_NUMBER,
        sender=SENDER,
        to=recipient,
        value=value,
        nonce=nonce,
        gas_limit=gas_limit,
        input_data=input_data,
    )


class TestBlock:
    def test_constructs_valid_block_and_normalizes_timestamp_to_utc(self) -> None:
        source_timezone = timezone(timedelta(hours=3))
        block = Block(
            number=BLOCK_NUMBER,
            hash=BLOCK_HASH,
            parent_hash=PARENT_HASH,
            timestamp=datetime(2024, 1, 1, 3, tzinfo=source_timezone),
        )

        assert block.number == BLOCK_NUMBER
        assert block.hash == BLOCK_HASH
        assert block.parent_hash == PARENT_HASH
        assert block.timestamp == datetime(2024, 1, 1, tzinfo=UTC)
        assert block.timestamp.tzinfo is UTC

    def test_rejects_naive_timestamp(self) -> None:
        with pytest.raises(DomainValidationError, match="must include timezone"):
            Block(
                number=BLOCK_NUMBER,
                hash=BLOCK_HASH,
                parent_hash=PARENT_HASH,
                timestamp=datetime(2024, 1, 1),
            )

    def test_rejects_timestamp_before_unix_epoch(self) -> None:
        with pytest.raises(DomainValidationError, match="before the Unix epoch"):
            Block(
                number=BLOCK_NUMBER,
                hash=BLOCK_HASH,
                parent_hash=PARENT_HASH,
                timestamp=datetime(1969, 12, 31, 23, 59, 59, tzinfo=UTC),
            )

    def test_is_immutable(self) -> None:
        block = Block(
            number=BLOCK_NUMBER,
            hash=BLOCK_HASH,
            parent_hash=PARENT_HASH,
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        )

        with pytest.raises(FrozenInstanceError):
            block.number = BlockNumber(1)  # type: ignore[misc]


class TestTransaction:
    def test_constructs_normal_eth_transfer(self) -> None:
        transaction = make_transaction()

        assert transaction.hash == TRANSACTION_HASH
        assert transaction.block_number == BLOCK_NUMBER
        assert transaction.sender == SENDER
        assert transaction.to == RECIPIENT
        assert transaction.value == Wei(10**18)
        assert transaction.nonce == 7
        assert transaction.gas_limit == 21_000
        assert transaction.input_data == b""

    def test_allows_contract_creation_without_recipient(self) -> None:
        creation_code = bytes.fromhex("60006000f3")

        transaction = make_transaction(recipient=None, input_data=creation_code)

        assert transaction.to is None
        assert transaction.input_data == creation_code

    def test_allows_zero_value_transaction(self) -> None:
        assert make_transaction(value=Wei(0)).value == Wei(0)

    @pytest.mark.parametrize("invalid_nonce", [-1, True])
    def test_rejects_invalid_nonce(self, invalid_nonce: int) -> None:
        with pytest.raises(DomainValidationError, match="Transaction nonce"):
            make_transaction(nonce=invalid_nonce)

    @pytest.mark.parametrize("invalid_gas_limit", [-1, True])
    def test_rejects_invalid_gas_limit(self, invalid_gas_limit: int) -> None:
        with pytest.raises(DomainValidationError, match="Transaction gas limit"):
            make_transaction(gas_limit=invalid_gas_limit)

    def test_is_immutable(self) -> None:
        transaction = make_transaction()

        with pytest.raises(FrozenInstanceError):
            transaction.nonce = 8  # type: ignore[misc]


class TestTransactionReceipt:
    def test_constructs_successful_receipt(self) -> None:
        receipt = TransactionReceipt(
            transaction_hash=TRANSACTION_HASH,
            block_number=BLOCK_NUMBER,
            succeeded=True,
            gas_used=20_000,
        )

        assert receipt.succeeded is True
        assert receipt.gas_used == 20_000

    def test_constructs_failed_receipt(self) -> None:
        receipt = TransactionReceipt(
            transaction_hash=TRANSACTION_HASH,
            block_number=BLOCK_NUMBER,
            succeeded=False,
            gas_used=21_000,
        )

        assert receipt.succeeded is False

    def test_allows_zero_gas_used(self) -> None:
        receipt = TransactionReceipt(
            transaction_hash=TRANSACTION_HASH,
            block_number=BLOCK_NUMBER,
            succeeded=True,
            gas_used=0,
        )

        assert receipt.gas_used == 0

    @pytest.mark.parametrize("invalid_gas_used", [-1, True])
    def test_rejects_invalid_gas_used(self, invalid_gas_used: int) -> None:
        with pytest.raises(DomainValidationError, match="Receipt gas used"):
            TransactionReceipt(
                transaction_hash=TRANSACTION_HASH,
                block_number=BLOCK_NUMBER,
                succeeded=True,
                gas_used=invalid_gas_used,
            )

    def test_is_immutable(self) -> None:
        receipt = TransactionReceipt(
            transaction_hash=TRANSACTION_HASH,
            block_number=BLOCK_NUMBER,
            succeeded=True,
            gas_used=20_000,
        )

        with pytest.raises(FrozenInstanceError):
            receipt.gas_used = 1  # type: ignore[misc]
