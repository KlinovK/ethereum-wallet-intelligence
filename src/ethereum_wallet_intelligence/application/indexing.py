"""Application models used to coordinate blockchain indexing."""

from dataclasses import dataclass

from ethereum_wallet_intelligence.domain.ethereum import BlockHash, BlockNumber, ChainId


@dataclass(frozen=True, slots=True)
class IndexerCheckpoint:
    """Durable position immediately before the next block to index."""

    chain_id: ChainId
    start_block_number: BlockNumber
    next_block_number: BlockNumber
    expected_parent_hash: BlockHash

    def __post_init__(self) -> None:
        if not isinstance(self.chain_id, ChainId):
            raise ValueError("Checkpoint chain ID must be a ChainId")
        if not isinstance(self.start_block_number, BlockNumber):
            raise ValueError("Checkpoint start block number must be a BlockNumber")
        if not isinstance(self.next_block_number, BlockNumber):
            raise ValueError("Checkpoint next block number must be a BlockNumber")
        if not isinstance(self.expected_parent_hash, BlockHash):
            raise ValueError("Checkpoint expected parent hash must be a BlockHash")
        if self.next_block_number.value < self.start_block_number.value:
            raise ValueError("Checkpoint next block number cannot precede its start block number")
