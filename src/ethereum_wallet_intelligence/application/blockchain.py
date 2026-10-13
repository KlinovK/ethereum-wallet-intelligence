"""Application-facing Ethereum blockchain port."""

from dataclasses import dataclass
from typing import Protocol

from ethereum_wallet_intelligence.domain.ethereum import (
    Block,
    BlockNumber,
    ChainId,
    EthereumAddress,
    Transaction,
    TransactionHash,
    TransactionReceipt,
    Wei,
)


@dataclass(frozen=True, slots=True)
class BlockBundle:
    """A complete block and its transactions, ready for indexing."""

    block: Block
    transactions: tuple[Transaction, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.block, Block):
            raise ValueError("Block bundle block must be a Block")
        if type(self.transactions) is not tuple:
            raise ValueError("Block bundle transactions must be a tuple")

        transaction_hashes: set[TransactionHash] = set()
        for transaction in self.transactions:
            if not isinstance(transaction, Transaction):
                raise ValueError("Block bundle transactions must contain Transaction values")
            if transaction.block_number != self.block.number:
                raise ValueError("Block bundle transaction belongs to a different block")
            if transaction.hash in transaction_hashes:
                raise ValueError("Block bundle contains duplicate transaction hashes")
            transaction_hashes.add(transaction.hash)


class BlockchainResourceNotFoundError(LookupError):
    """Raised when a requested blockchain resource does not exist."""


class BlockchainProviderError(RuntimeError):
    """Raised when the configured blockchain provider cannot fulfill a request."""


class TransientBlockchainProviderError(BlockchainProviderError):
    """Raised for a provider read failure that is safe to retry."""


class PermanentBlockchainProviderError(BlockchainProviderError):
    """Raised for a provider failure that must not be retried."""


class InvalidBlockchainResponseError(PermanentBlockchainProviderError):
    """Raised when a provider response cannot form valid application data."""


class BlockchainGateway(Protocol):
    """Read-only blockchain operations required by the application."""

    async def get_chain_id(self) -> ChainId: ...

    async def get_current_block_number(self) -> BlockNumber: ...

    async def get_block(self, number: BlockNumber) -> Block: ...

    async def get_block_with_transactions(self, number: BlockNumber) -> BlockBundle: ...

    async def get_balance(self, address: EthereumAddress) -> Wei: ...

    async def get_transaction(self, transaction_hash: TransactionHash) -> Transaction: ...

    async def get_transaction_receipt(
        self,
        transaction_hash: TransactionHash,
    ) -> TransactionReceipt: ...
