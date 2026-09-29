"""Application-facing Ethereum blockchain port."""

from typing import Protocol

from ethereum_wallet_intelligence.domain.ethereum import (
    Block,
    BlockNumber,
    EthereumAddress,
    Transaction,
    TransactionHash,
    TransactionReceipt,
    Wei,
)


class BlockchainResourceNotFoundError(LookupError):
    """Raised when a requested blockchain resource does not exist."""


class BlockchainProviderError(RuntimeError):
    """Raised when the configured blockchain provider cannot fulfill a request."""


class BlockchainGateway(Protocol):
    """Read-only blockchain operations required by the application."""

    async def get_current_block_number(self) -> BlockNumber: ...

    async def get_block(self, number: BlockNumber) -> Block: ...

    async def get_balance(self, address: EthereumAddress) -> Wei: ...

    async def get_transaction(self, transaction_hash: TransactionHash) -> Transaction: ...

    async def get_transaction_receipt(
        self,
        transaction_hash: TransactionHash,
    ) -> TransactionReceipt: ...
