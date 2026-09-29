"""Manually invoked read-only Ethereum JSON-RPC smoke check."""

import asyncio

from ethereum_wallet_intelligence.application import (
    BlockchainProviderError,
    BlockchainResourceNotFoundError,
)
from ethereum_wallet_intelligence.config import Settings
from ethereum_wallet_intelligence.infrastructure import Web3BlockchainGateway


async def run_rpc_smoke() -> None:
    """Read the latest block and print a small sanitized summary."""
    settings = Settings()
    gateway = Web3BlockchainGateway.from_rpc_url(settings.require_ethereum_rpc_url())

    current_block_number = await gateway.get_current_block_number()
    block = await gateway.get_block(current_block_number)

    print(f"Current block number: {current_block_number.value}")
    print(f"Block hash: {block.hash}")
    print(f"Parent hash: {block.parent_hash}")
    print(f"UTC timestamp: {block.timestamp.isoformat()}")


def main() -> None:
    """Run the smoke check and present expected failures without sensitive configuration."""
    try:
        asyncio.run(run_rpc_smoke())
    except (
        ValueError,
        BlockchainProviderError,
        BlockchainResourceNotFoundError,
    ) as error:
        raise SystemExit(f"Ethereum RPC smoke check failed: {error}") from None


if __name__ == "__main__":
    main()
