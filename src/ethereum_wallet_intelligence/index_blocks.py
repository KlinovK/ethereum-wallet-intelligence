"""Explicit operator CLI for bounded Ethereum block indexing."""

import argparse
import asyncio
from collections.abc import Sequence

from ethereum_wallet_intelligence.application import (
    BlockchainProviderError,
    BlockchainResourceNotFoundError,
    BlockIndexer,
    BlockIndexingError,
    PersistenceError,
)
from ethereum_wallet_intelligence.config import Settings
from ethereum_wallet_intelligence.domain.ethereum import BlockNumber
from ethereum_wallet_intelligence.infrastructure import Web3BlockchainGateway
from ethereum_wallet_intelligence.infrastructure.persistence import (
    SQLAlchemyIndexerUnitOfWork,
    create_database_engine,
    create_session_factory,
)


def _block_number(raw_value: str) -> BlockNumber:
    try:
        return BlockNumber(int(raw_value))
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be a non-negative integer") from error


def _parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Index an explicit inclusive Ethereum Mainnet block range.",
    )
    parser.add_argument("--start", required=True, type=_block_number, help="inclusive start block")
    parser.add_argument("--end", required=True, type=_block_number, help="inclusive end block")
    return parser.parse_args(arguments)


async def run_indexing(
    start: BlockNumber,
    end: BlockNumber,
    settings: Settings | None = None,
) -> int:
    """Compose infrastructure and run one explicitly requested indexing range."""
    resolved_settings = settings or Settings()
    gateway = Web3BlockchainGateway.from_rpc_url(resolved_settings.require_ethereum_rpc_url())
    engine = create_database_engine(resolved_settings.require_database_url())
    session_factory = create_session_factory(engine)
    indexer = BlockIndexer(
        gateway,
        lambda: SQLAlchemyIndexerUnitOfWork(session_factory),
    )
    try:
        return await indexer.index_range(start, end)
    finally:
        await engine.dispose()


def main(arguments: Sequence[str] | None = None) -> None:
    """Run one bounded range and report only credential-safe progress."""
    args = _parse_args(arguments)
    start: BlockNumber = args.start
    end: BlockNumber = args.end
    print(f"Indexing Ethereum Mainnet blocks {start.value} through {end.value}...")
    try:
        indexed = asyncio.run(run_indexing(start, end))
    except (
        BlockIndexingError,
        BlockchainProviderError,
        BlockchainResourceNotFoundError,
        PersistenceError,
        ValueError,
    ) as error:
        raise SystemExit(f"Block indexing failed: {error}") from None
    except Exception:
        raise SystemExit("Block indexing failed due to an unexpected error") from None

    print(f"Completed: {indexed} new block(s) committed.")


if __name__ == "__main__":
    main()
