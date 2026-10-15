"""Tests for the explicit block-indexing CLI."""

from collections.abc import Sequence

import pytest

from ethereum_wallet_intelligence import index_blocks
from ethereum_wallet_intelligence.application import (
    CommitReconciliationError,
    IndexingConsistencyError,
    PermanentBlockchainProviderError,
    ReorganizationDepthExceededError,
    RpcRetryExhaustedError,
)
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


@pytest.mark.parametrize(
    ("failure", "exit_code", "category"),
    [
        (RpcRetryExhaustedError("safe"), 3, "transient RPC exhausted"),
        (IndexingConsistencyError("safe"), 4, "consistency"),
        (ReorganizationDepthExceededError("safe"), 4, "consistency"),
        (CommitReconciliationError("safe"), 5, "commit recovery"),
        (PermanentBlockchainProviderError("safe"), 6, "permanent RPC"),
    ],
)
def test_index_cli_reports_failure_category_and_exit_code(
    failure: Exception,
    exit_code: int,
    category: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fail(
        start: BlockNumber,
        end: BlockNumber,
        settings: object | None = None,
    ) -> int:
        del start, end, settings
        raise failure

    monkeypatch.setattr(index_blocks, "run_indexing", fail)
    arguments: Sequence[str] = ("--start", "10", "--end", "10")

    with pytest.raises(SystemExit) as error:
        index_blocks.main(arguments)

    assert error.value.code == exit_code
    assert f"[{category}]" in capsys.readouterr().err
