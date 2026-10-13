"""Bounded sequential Ethereum block indexing use case."""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import NoReturn, TypeVar

from ethereum_wallet_intelligence.application.blockchain import (
    BlockBundle,
    BlockchainGateway,
    TransientBlockchainProviderError,
)
from ethereum_wallet_intelligence.application.indexing import IndexerCheckpoint
from ethereum_wallet_intelligence.application.repositories import CheckpointConflictError
from ethereum_wallet_intelligence.application.unit_of_work import (
    CommitOutcomeUnknownError,
    IndexerUnitOfWork,
)
from ethereum_wallet_intelligence.domain.ethereum import (
    BlockHash,
    BlockNumber,
    ChainId,
    Transaction,
)

MAX_BLOCKS_PER_INVOCATION = 100
_GENESIS_PARENT_HASH = BlockHash("0x" + "00" * 32)
_MAINNET_CHAIN_ID = ChainId.ethereum_mainnet()
_ResultT = TypeVar("_ResultT")


class InvalidBlockRangeError(ValueError):
    """Raised when a requested indexing range is invalid or unsafe."""


class BlockIndexingError(RuntimeError):
    """Raised when indexing cannot safely continue."""


class RpcRetryExhaustedError(BlockIndexingError):
    """Raised after all safe attempts for a transient RPC read have failed."""


class IndexingConsistencyError(BlockIndexingError):
    """Raised when durable persistence state is not internally consistent."""


class ChainContinuityError(IndexingConsistencyError):
    """Raised when a fetched block does not extend the expected canonical tip."""


class CommitReconciliationError(IndexingConsistencyError):
    """Raised when an uncertain database commit cannot be proven safe."""


class CommitNotDurableError(CommitReconciliationError):
    """Raised when reconciliation proves that an uncertain commit did not persist."""


@dataclass(frozen=True, slots=True)
class RpcRetryPolicy:
    """Bounded retry settings for transient read-only RPC failures."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 2.0
    jitter_ratio: float = 0.2

    def __post_init__(self) -> None:
        if type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 5:
            raise ValueError("RPC retry max attempts must be between 1 and 5")
        if not 0 <= self.base_delay_seconds <= 5:
            raise ValueError("RPC retry base delay must be between 0 and 5 seconds")
        if not 0 <= self.max_delay_seconds <= 30:
            raise ValueError("RPC retry maximum delay must be between 0 and 30 seconds")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("RPC retry maximum delay cannot be below its base delay")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("RPC retry jitter ratio must be between 0 and 1")


class BlockIndexer:
    """Index an explicit bounded range one atomic block at a time."""

    def __init__(
        self,
        blockchain: BlockchainGateway,
        unit_of_work_factory: Callable[[], IndexerUnitOfWork],
        *,
        retry_policy: RpcRetryPolicy | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        logger: logging.Logger | None = None,
    ) -> None:
        self._blockchain = blockchain
        self._unit_of_work_factory = unit_of_work_factory
        self._retry_policy = retry_policy or RpcRetryPolicy()
        self._sleep = sleep
        self._jitter = jitter
        self._logger = logger or logging.getLogger(__name__)

    async def index_range(self, start: BlockNumber, end: BlockNumber) -> int:
        """Index the inclusive range and return the number of newly persisted blocks."""
        self._validate_range(start, end)
        self._logger.info(
            "indexing_started",
            extra={"event": "indexing_started", "start_block": start.value, "end_block": end.value},
        )

        provider_chain_id = await self._rpc_read("chain_id", self._blockchain.get_chain_id)
        if provider_chain_id != _MAINNET_CHAIN_ID:
            raise BlockIndexingError("The block indexer supports Ethereum Mainnet only")

        checkpoint = await self._load_or_initialize_checkpoint(start, provider_chain_id)
        self._validate_checkpoint_for_request(checkpoint, provider_chain_id, start)
        await self._validate_startup_consistency(checkpoint)

        indexed_blocks = 0
        while checkpoint.next_block_number.value <= end.value:
            requested_number = checkpoint.next_block_number
            bundle = await self._rpc_read(
                "block_with_transactions",
                partial(self._blockchain.get_block_with_transactions, requested_number),
            )
            self._validate_bundle(bundle, checkpoint, requested_number)
            checkpoint = await self._persist_bundle(bundle, checkpoint, provider_chain_id)
            indexed_blocks += 1

        self._logger.info(
            "indexing_finished",
            extra={
                "event": "indexing_finished",
                "indexed_blocks": indexed_blocks,
                "next_block": checkpoint.next_block_number.value,
            },
        )
        return indexed_blocks

    async def _rpc_read(
        self,
        operation: str,
        read: Callable[[], Awaitable[_ResultT]],
    ) -> _ResultT:
        policy = self._retry_policy
        for attempt in range(1, policy.max_attempts + 1):
            try:
                return await read()
            except TransientBlockchainProviderError as error:
                if attempt == policy.max_attempts:
                    raise RpcRetryExhaustedError(
                        f"Transient Ethereum RPC failure exhausted {policy.max_attempts} attempts"
                    ) from error
                exponential = policy.base_delay_seconds * 2 ** (attempt - 1)
                jitter_value = min(1.0, max(0.0, self._jitter()))
                delay = min(
                    policy.max_delay_seconds,
                    exponential * (1 + policy.jitter_ratio * jitter_value),
                )
                self._logger.warning(
                    "rpc_retry_scheduled",
                    extra={
                        "event": "rpc_retry_scheduled",
                        "operation": operation,
                        "attempt": attempt,
                        "max_attempts": policy.max_attempts,
                        "delay_seconds": delay,
                    },
                )
                await self._sleep(delay)
        raise AssertionError("RPC retry loop terminated unexpectedly")

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
            self._log_checkpoint_resume(existing)
            return existing

        if start.value == 0:
            expected_parent_hash = _GENESIS_PARENT_HASH
        else:
            anchor_number = BlockNumber(start.value - 1)
            anchor = await self._rpc_read(
                "checkpoint_anchor",
                lambda: self._blockchain.get_block(anchor_number),
            )
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
                    self._log_checkpoint_resume(concurrent)
                    return concurrent
                await self._reject_orphaned_data(unit_of_work)
                await unit_of_work.checkpoints.create(initial)
                await unit_of_work.commit()
                return initial
        except CommitOutcomeUnknownError:
            return await self._reconcile_checkpoint_initialization(initial)
        except CheckpointConflictError:
            concurrent = await self._inspect_checkpoint_state()
            if concurrent is None:
                raise
            self._log_checkpoint_resume(concurrent)
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
            raise IndexingConsistencyError(
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

    async def _validate_startup_consistency(self, expected: IndexerCheckpoint) -> None:
        async with self._unit_of_work_factory() as unit_of_work:
            durable = await unit_of_work.checkpoints.get_for_update()
            if durable != expected:
                self._raise_consistency_error(
                    "Checkpoint changed during startup consistency validation",
                    "checkpoint_changed",
                )

            highest = await unit_of_work.blocks.get_highest_number()
            if expected.next_block_number == expected.start_block_number:
                if highest is not None:
                    self._raise_consistency_error(
                        "Persisted blocks exist beyond an unstarted checkpoint",
                        "unexpected_blocks",
                    )
                return

            tip_number = BlockNumber(expected.next_block_number.value - 1)
            if highest is not None and highest.value > tip_number.value:
                self._raise_consistency_error(
                    "Persisted blocks exist beyond the durable checkpoint",
                    "unexpected_blocks",
                )
            tip = await unit_of_work.blocks.get_by_number(tip_number)
            if tip is None:
                self._raise_consistency_error(
                    "The block referenced by the durable checkpoint is missing",
                    "missing_tip",
                )
            if tip.hash != expected.expected_parent_hash:
                self._raise_consistency_error(
                    "The durable checkpoint parent hash does not match its persisted tip",
                    "broken_parent_hash",
                )

    def _raise_consistency_error(self, message: str, reason: str) -> NoReturn:
        self._logger.error(
            "indexing_consistency_failure",
            extra={"event": "indexing_consistency_failure", "reason": reason},
        )
        raise IndexingConsistencyError(message)

    @staticmethod
    def _validate_bundle(
        bundle: BlockBundle,
        checkpoint: IndexerCheckpoint,
        requested_number: BlockNumber,
    ) -> None:
        if bundle.block.number != requested_number:
            raise BlockIndexingError("Provider returned a different block height than requested")
        if bundle.block.parent_hash != checkpoint.expected_parent_hash:
            raise ChainContinuityError(
                "Block parent hash does not match the durable checkpoint; possible reorganization"
            )

    async def _persist_bundle(
        self,
        bundle: BlockBundle,
        expected: IndexerCheckpoint,
        provider_chain_id: ChainId,
    ) -> IndexerCheckpoint:
        intended = IndexerCheckpoint(
            chain_id=expected.chain_id,
            start_block_number=expected.start_block_number,
            next_block_number=BlockNumber(bundle.block.number.value + 1),
            expected_parent_hash=bundle.block.hash,
        )
        try:
            async with self._unit_of_work_factory() as unit_of_work:
                locked = await unit_of_work.checkpoints.get_for_update()
                if locked is None:
                    raise IndexingConsistencyError(
                        "The indexing checkpoint disappeared during indexing"
                    )
                if locked.chain_id != provider_chain_id:
                    raise BlockIndexingError("Provider and checkpoint chain IDs do not match")
                if locked != expected:
                    raise BlockIndexingError("The indexing checkpoint changed during RPC retrieval")

                self._validate_bundle(bundle, locked, locked.next_block_number)
                await unit_of_work.blocks.save(bundle.block)
                for transaction in bundle.transactions:
                    await unit_of_work.transactions.save(transaction)
                advanced = await unit_of_work.checkpoints.advance(locked, bundle.block)
                if advanced != intended:
                    raise IndexingConsistencyError(
                        "Checkpoint repository returned an unexpected transition"
                    )
                await unit_of_work.commit()
                return advanced
        except CommitOutcomeUnknownError:
            return await self._reconcile_bundle_commit(bundle, expected, intended)

    async def _reconcile_checkpoint_initialization(
        self,
        intended: IndexerCheckpoint,
    ) -> IndexerCheckpoint:
        self._logger.warning(
            "commit_reconciliation_started",
            extra={"event": "commit_reconciliation_started", "operation": "checkpoint_create"},
        )
        async with self._unit_of_work_factory() as unit_of_work:
            durable = await unit_of_work.checkpoints.get_for_update()
            has_data = (
                await unit_of_work.blocks.has_any() or await unit_of_work.transactions.has_any()
            )
            if durable == intended and not has_data:
                self._log_reconciliation("committed", "checkpoint_create")
                return intended
            if durable is None and not has_data:
                self._log_reconciliation("not_durable", "checkpoint_create")
                raise CommitNotDurableError(
                    "Checkpoint initialization was not durable; retry the same range"
                )
            self._log_reconciliation("inconsistent", "checkpoint_create")
            raise CommitReconciliationError(
                "Checkpoint initialization commit left ambiguous durable state"
            )

    async def _reconcile_bundle_commit(
        self,
        bundle: BlockBundle,
        expected: IndexerCheckpoint,
        intended: IndexerCheckpoint,
    ) -> IndexerCheckpoint:
        self._logger.warning(
            "commit_reconciliation_started",
            extra={"event": "commit_reconciliation_started", "operation": "block_commit"},
        )
        async with self._unit_of_work_factory() as unit_of_work:
            durable = await unit_of_work.checkpoints.get_for_update()
            persisted_block = await unit_of_work.blocks.get_by_number(bundle.block.number)
            persisted_transactions = await unit_of_work.transactions.get_by_block_number(
                bundle.block.number
            )

            if durable == intended:
                if persisted_block != bundle.block or not self._transactions_match(
                    bundle,
                    persisted_transactions,
                ):
                    self._log_reconciliation("inconsistent", "block_commit")
                    raise CommitReconciliationError(
                        "Checkpoint advanced but the committed block bundle is incomplete"
                    )
                self._log_reconciliation("committed", "block_commit")
                return intended

            if durable == expected:
                if persisted_block is not None or persisted_transactions:
                    self._log_reconciliation("inconsistent", "block_commit")
                    raise CommitReconciliationError(
                        "Checkpoint did not advance but partial block data is durable"
                    )
                self._log_reconciliation("not_durable", "block_commit")
                raise CommitNotDurableError("Block commit was not durable; retry the same range")

            self._log_reconciliation("inconsistent", "block_commit")
            raise CommitReconciliationError(
                "Database commit outcome cannot be reconciled with the expected checkpoint"
            )

    @staticmethod
    def _transactions_match(
        bundle: BlockBundle,
        persisted: tuple[Transaction, ...],
    ) -> bool:
        expected_by_hash = {transaction.hash: transaction for transaction in bundle.transactions}
        return len(persisted) == len(expected_by_hash) and all(
            transaction.hash in expected_by_hash
            and expected_by_hash[transaction.hash] == transaction
            for transaction in persisted
        )

    def _log_checkpoint_resume(self, checkpoint: IndexerCheckpoint) -> None:
        self._logger.info(
            "checkpoint_resumed",
            extra={
                "event": "checkpoint_resumed",
                "next_block": checkpoint.next_block_number.value,
            },
        )

    def _log_reconciliation(self, outcome: str, operation: str) -> None:
        self._logger.warning(
            "commit_reconciliation_finished",
            extra={
                "event": "commit_reconciliation_finished",
                "operation": operation,
                "outcome": outcome,
            },
        )
