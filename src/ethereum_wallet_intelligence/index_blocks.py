"""Explicit operator CLI for bounded Ethereum block indexing."""

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

from ethereum_wallet_intelligence.application import (
    BlockchainProviderError,
    BlockchainResourceNotFoundError,
    BlockIndexer,
    BlockIndexingError,
    CommitReconciliationError,
    IndexingConsistencyError,
    PermanentBlockchainProviderError,
    PersistenceError,
    RpcRetryExhaustedError,
    RpcRetryPolicy,
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
    gateway = Web3BlockchainGateway.from_rpc_url(
        resolved_settings.require_ethereum_rpc_url(),
        request_timeout_seconds=resolved_settings.ethereum_rpc_timeout_seconds,
    )
    engine = create_database_engine(resolved_settings.require_database_url())
    session_factory = create_session_factory(engine)
    indexer = BlockIndexer(
        gateway,
        lambda: SQLAlchemyIndexerUnitOfWork(session_factory),
        retry_policy=RpcRetryPolicy(
            max_attempts=resolved_settings.ethereum_rpc_max_attempts,
            base_delay_seconds=resolved_settings.ethereum_rpc_retry_base_delay_seconds,
            max_delay_seconds=resolved_settings.ethereum_rpc_retry_max_delay_seconds,
            jitter_ratio=resolved_settings.ethereum_rpc_retry_jitter_ratio,
        ),
    )
    try:
        return await indexer.index_range(start, end)
    finally:
        await engine.dispose()


def main(arguments: Sequence[str] | None = None) -> None:
    """Run one bounded range and report only credential-safe progress."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args(arguments)
    start: BlockNumber = args.start
    end: BlockNumber = args.end
    print(f"Indexing Ethereum Mainnet blocks {start.value} through {end.value}...")
    try:
        indexed = asyncio.run(run_indexing(start, end))
    except RpcRetryExhaustedError as error:
        print(f"Block indexing failed [transient RPC exhausted]: {error}", file=sys.stderr)
        raise SystemExit(3) from None
    except CommitReconciliationError as error:
        print(f"Block indexing failed [commit recovery]: {error}", file=sys.stderr)
        raise SystemExit(5) from None
    except IndexingConsistencyError as error:
        print(f"Block indexing failed [consistency]: {error}", file=sys.stderr)
        raise SystemExit(4) from None
    except (PermanentBlockchainProviderError, BlockchainResourceNotFoundError) as error:
        print(f"Block indexing failed [permanent RPC]: {error}", file=sys.stderr)
        raise SystemExit(6) from None
    except (
        BlockIndexingError,
        BlockchainProviderError,
        PersistenceError,
        ValueError,
    ) as error:
        print(f"Block indexing failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print("Block indexing failed due to an unexpected error", file=sys.stderr)
        raise SystemExit(1) from None

    print(f"Completed: {indexed} new block(s) committed.")


if __name__ == "__main__":
    main()
