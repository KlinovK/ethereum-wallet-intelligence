"""Tests for the explicit block-indexing CLI."""

import pytest

from ethereum_wallet_intelligence.domain.ethereum import BlockNumber
from ethereum_wallet_intelligence.index_blocks import _parse_args


def test_index_cli_requires_explicit_start_and_end() -> None:
    with pytest.raises(SystemExit):
        _parse_args([])


def test_index_cli_parses_non_negative_block_numbers() -> None:
    arguments = _parse_args(["--start", "10", "--end", "19"])

    assert arguments.start == BlockNumber(10)
    assert arguments.end == BlockNumber(19)


def test_index_cli_rejects_negative_block_number() -> None:
    with pytest.raises(SystemExit):
        _parse_args(["--start", "-1", "--end", "10"])
