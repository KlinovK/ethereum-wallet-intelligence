"""Bounded sequential Ethereum block indexing use case."""

from collections.abc import Callable

from ethereum_wallet_intelligence.application.blockchain import BlockBundle, BlockchainGateway
from ethereum_wallet_intelligence.application.indexing import IndexerCheckpoint
from ethereum_wallet_intelligence.application.repositories import CheckpointConflictError
from ethereum_wallet_intelligence.application.unit_of_work import IndexerUnitOfWork
from ethereum_wallet_intelligence.domain.ethereum import BlockHash, BlockNumber, ChainId

MAX_BLOCKS_PER_INVOCATION = 100
_GENESIS_PARENT_HASH = BlockHash("0x" + "00" * 32)
_MAINNET_CHAIN_ID = ChainId.ethereum_mainnet()


class InvalidBlockRangeError(ValueError):
    """Raised when a requested indexing range is invalid or unsafe."""


class BlockIndexingError(RuntimeError):
    """Raised when indexing cannot safely continue."""


class BlockIndexer:
    """Index an explicit bounded range one atomic block at a time."""

    def __init__(
        self,
        blockchain: BlockchainGateway,
        unit_of_work_factory: Callable[[], IndexerUnitOfWork],
    ) -> None:
        self._blockchain = blockchain
        self._unit_of_work_factory = unit_of_work_factory

    async def index_range(self, start: BlockNumber, end: BlockNumber) -> int:
        """Index the inclusive range and return the number of newly persisted blocks."""
        self._validate_range(start, end)

        provider_chain_id = await self._blockchain.get_chain_id()
        if provider_chain_id != _MAINNET_CHAIN_ID:
            raise BlockIndexingError("The block indexer supports Ethereum Mainnet only")

        checkpoint = await self._load_or_initialize_checkpoint(start, provider_chain_id)
        self._validate_checkpoint_for_request(checkpoint, provider_chain_id, start)

        indexed_blocks = 0
        while checkpoint.next_block_number.value <= end.value:
            requested_number = checkpoint.next_block_number
            bundle = await self._blockchain.get_block_with_transactions(requested_number)
            self._validate_bundle(bundle, checkpoint, requested_number)
            checkpoint = await self._persist_bundle(bundle, checkpoint, provider_chain_id)
            indexed_blocks += 1

        return indexed_blocks

    @staticmethod
    def _validate_range(start: BlockNumber, end: BlockNumber) -> None:
        if not isinstance(start, BlockNumber) or not isinstance(end, BlockNumber):
            raise InvalidBlockRangeError("Index range boundaries must be BlockNumber values")
        if end.value < start.value:
            raise InvalidBlockRangeError("Index range end cannot precede start")
        if end.value - start.value + 1 > MAX_BLOCKS_PER_INVOCATION:
            raise InvalidBlockRangeError(
                f"Index range cannot exceed {MAX_BLOCKS_PER_INVOCATION} blocks"
            )

    async def _load_or_initialize_checkpoint(
        self,
        start: BlockNumber,
        provider_chain_id: ChainId,
    ) -> IndexerCheckpoint:
        existing = await self._inspect_checkpoint_state()
        if existing is not None:
            return existing

        if start.value == 0:
            expected_parent_hash = _GENESIS_PARENT_HASH
        else:
            anchor_number = BlockNumber(start.value - 1)
            anchor = await self._blockchain.get_block(anchor_number)
            if anchor.number != anchor_number:
                raise BlockIndexingError("Provider returned an incorrect checkpoint anchor height")
            expected_parent_hash = anchor.hash

        initial = IndexerCheckpoint(
            chain_id=provider_chain_id,
            start_block_number=start,
            next_block_number=start,
            expected_parent_hash=expected_parent_hash,
        )
        try:
            async with self._unit_of_work_factory() as unit_of_work:
                concurrent = await unit_of_work.checkpoints.get_for_update()
                if concurrent is not None:
                    return concurrent
                await self._reject_orphaned_data(unit_of_work)
                await unit_of_work.checkpoints.create(initial)
                await unit_of_work.commit()
                return initial
        except CheckpointConflictError:
            concurrent = await self._inspect_checkpoint_state()
            if concurrent is None:
                raise
            return concurrent

    async def _inspect_checkpoint_state(self) -> IndexerCheckpoint | None:
        async with self._unit_of_work_factory() as unit_of_work:
            checkpoint = await unit_of_work.checkpoints.get_for_update()
            if checkpoint is None:
                await self._reject_orphaned_data(unit_of_work)
            return checkpoint

    @staticmethod
    async def _reject_orphaned_data(unit_of_work: IndexerUnitOfWork) -> None:
        if await unit_of_work.blocks.has_any() or await unit_of_work.transactions.has_any():
            raise BlockIndexingError(
                "Indexed block or transaction data exists without an indexing checkpoint"
            )

    @staticmethod
    def _validate_checkpoint_for_request(
        checkpoint: IndexerCheckpoint,
        provider_chain_id: ChainId,
        requested_start: BlockNumber,
    ) -> None:
        if checkpoint.chain_id != provider_chain_id:
            raise BlockIndexingError("Provider and checkpoint chain IDs do not match")
        if checkpoint.chain_id != _MAINNET_CHAIN_ID:
            raise BlockIndexingError("The persisted checkpoint is not for Ethereum Mainnet")
        if requested_start.value < checkpoint.start_block_number.value:
            raise BlockIndexingError("Requested range begins before the checkpoint start boundary")
        if requested_start.value > checkpoint.next_block_number.value:
            raise BlockIndexingError("Requested range would skip unindexed block heights")

    @staticmethod
    def _validate_bundle(
        bundle: BlockBundle,
        checkpoint: IndexerCheckpoint,
        requested_number: BlockNumber,
    ) -> None:
        if bundle.block.number != requested_number:
            raise BlockIndexingError("Provider returned a different block height than requested")
        if bundle.block.parent_hash != checkpoint.expected_parent_hash:
            raise BlockIndexingError(
                "Block parent hash does not match the durable checkpoint; possible reorganization"
            )

    async def _persist_bundle(
        self,
        bundle: BlockBundle,
        expected: IndexerCheckpoint,
        provider_chain_id: ChainId,
    ) -> IndexerCheckpoint:
        async with self._unit_of_work_factory() as unit_of_work:
            locked = await unit_of_work.checkpoints.get_for_update()
            if locked is None:
                raise BlockIndexingError("The indexing checkpoint disappeared during indexing")
            if locked.chain_id != provider_chain_id:
                raise BlockIndexingError("Provider and checkpoint chain IDs do not match")
            if locked != expected:
                raise BlockIndexingError("The indexing checkpoint changed during RPC retrieval")

            self._validate_bundle(bundle, locked, locked.next_block_number)
            await unit_of_work.blocks.save(bundle.block)
            for transaction in bundle.transactions:
                await unit_of_work.transactions.save(transaction)
            advanced = await unit_of_work.checkpoints.advance(locked, bundle.block)
            await unit_of_work.commit()
            return advanced
