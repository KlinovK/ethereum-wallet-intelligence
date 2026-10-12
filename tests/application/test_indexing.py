"""Tests for application indexing models."""

from dataclasses import FrozenInstanceError

import pytest

from ethereum_wallet_intelligence.application import IndexerCheckpoint
from ethereum_wallet_intelligence.domain.ethereum import BlockHash, BlockNumber, ChainId

HASH = "0x" + "ab" * 32


def test_checkpoint_accepts_start_position_and_is_immutable() -> None:
    checkpoint = IndexerCheckpoint(
        chain_id=ChainId.ethereum_mainnet(),
        start_block_number=BlockNumber(100),
        next_block_number=BlockNumber(100),
        expected_parent_hash=BlockHash(HASH),
    )

    assert checkpoint.next_block_number == checkpoint.start_block_number
    with pytest.raises(FrozenInstanceError):
        checkpoint.next_block_number = BlockNumber(101)  # type: ignore[misc]


def test_checkpoint_accepts_position_after_start() -> None:
    checkpoint = IndexerCheckpoint(
        chain_id=ChainId(8453),
        start_block_number=BlockNumber(100),
        next_block_number=BlockNumber(125),
        expected_parent_hash=BlockHash(HASH),
    )

    assert checkpoint.chain_id == ChainId(8453)
    assert checkpoint.next_block_number == BlockNumber(125)


def test_checkpoint_rejects_next_block_before_start() -> None:
    with pytest.raises(ValueError, match="cannot precede"):
        IndexerCheckpoint(
            chain_id=ChainId.ethereum_mainnet(),
            start_block_number=BlockNumber(100),
            next_block_number=BlockNumber(99),
            expected_parent_hash=BlockHash(HASH),
        )
