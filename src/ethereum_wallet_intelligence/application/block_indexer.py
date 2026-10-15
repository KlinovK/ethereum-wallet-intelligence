"""Bounded sequential Ethereum block indexing use case."""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import NoReturn, TypeVar

from ethereum_wallet_intelligence.application.blockchain import (
    BlockBundle,
    BlockchainGateway,
    InvalidBlockchainResponseError,
    TransientBlockchainProviderError,
)
from ethereum_wallet_intelligence.application.indexing import IndexerCheckpoint
from ethereum_wallet_intelligence.application.repositories import CheckpointConflictError
from ethereum_wallet_intelligence.application.unit_of_work import (
    CommitOutcomeUnknownError,
    IndexerUnitOfWork,
)
from ethereum_wallet_intelligence.domain.ethereum import (
    Block,
    BlockHash,
    BlockNumber,
    ChainId,
    Transaction,
)

MAX_BLOCKS_PER_INVOCATION = 100
MAX_REORGANIZATION_DEPTH = 64
MAX_REORGANIZATION_RECOVERY_CYCLES = 3
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


class ReorganizationRecoveryError(IndexingConsistencyError):
    """Raised when a detected chain reorganization cannot be recovered safely."""


class ReorganizationDepthExceededError(ReorganizationRecoveryError):
    """Raised when no common ancestor exists within the automatic rewind bound."""


class ReorganizationStartBoundaryError(ReorganizationRecoveryError):
    """Raised when recovery would cross the original indexing start boundary."""


class ReorganizationPlanStaleError(ReorganizationRecoveryError):
    """Raised when durable state changes after a recovery plan is discovered."""


class UnstableCanonicalChainError(ReorganizationRecoveryError):
    """Raised when the provider chain does not stabilize during bounded recovery."""


class ReorganizationRangeError(ReorganizationRecoveryError):
    """Raised when recovery moves the checkpoint below the operator's requested range."""


class RewindCommitNotDurableError(CommitNotDurableError):
    """Raised when reconciliation proves that a rewind commit was not durable."""


@dataclass(frozen=True, slots=True)
class _LocalChainSnapshot:
    checkpoint: IndexerCheckpoint
    blocks_descending: tuple[Block, ...]


@dataclass(frozen=True, slots=True)
class _RewindSnapshot:
    expected: IndexerCheckpoint
    intended: IndexerCheckpoint
    ancestor: Block
    descendants: tuple[Block, ...]
    descendant_transactions: tuple[tuple[BlockNumber, tuple[Transaction, ...]], ...]


@dataclass(slots=True)
class _ReorganizationRecoveryBudget:
    """Invocation-wide allowance for complete reorganization recovery cycles.

    A cycle begins after a canonical checkpoint mismatch is observed and before
    discovery, rewind, or any other recovery action starts. Provider instability
    and a proven non-durable rewind therefore consume the same finite allowance
    as a successfully committed rewind.
    """

    cycles_started: int = 0

    def begin_cycle(self) -> int:
        if self.cycles_started >= MAX_REORGANIZATION_RECOVERY_CYCLES:
            raise UnstableCanonicalChainError(
                "Canonical Ethereum history did not stabilize within the "
                f"invocation-wide limit of {MAX_REORGANIZATION_RECOVERY_CYCLES} "
                "recovery cycles"
            )
        self.cycles_started += 1
        return self.cycles_started


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
        recovery_budget = _ReorganizationRecoveryBudget()
        checkpoint = await self._ensure_canonical_checkpoint(checkpoint, recovery_budget)
        self._validate_request_after_recovery(checkpoint, start)

        indexed_blocks = 0
        canonical_verified = True
        while True:
            if checkpoint.next_block_number.value > end.value:
                if canonical_verified:
                    break
                previous = checkpoint
                checkpoint = await self._ensure_canonical_checkpoint(
                    checkpoint,
                    recovery_budget,
                )
                self._validate_request_after_recovery(checkpoint, start)
                canonical_verified = True
                if checkpoint != previous:
                    continue
                break

            requested_number = checkpoint.next_block_number
            bundle = await self._rpc_read(
                "block_with_transactions",
                partial(self._blockchain.get_block_with_transactions, requested_number),
            )
            self._validate_bundle_height(bundle, requested_number)
            if bundle.block.parent_hash != checkpoint.expected_parent_hash:
                self._log_reorganization_detected(checkpoint, "parent_mismatch")
                previous = checkpoint
                checkpoint = await self._ensure_canonical_checkpoint(
                    checkpoint,
                    recovery_budget,
                )
                self._validate_request_after_recovery(checkpoint, start)
                if checkpoint == previous:
                    raise UnstableCanonicalChainError(
                        "Ethereum provider returned a block that does not extend its canonical tip"
                    )
                canonical_verified = True
                continue
            checkpoint = await self._persist_bundle(bundle, checkpoint, provider_chain_id)
            indexed_blocks += 1
            canonical_verified = False

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

    @staticmethod
    def _validate_bundle_height(bundle: BlockBundle, requested_number: BlockNumber) -> None:
        if bundle.block.number != requested_number:
            raise BlockIndexingError("Provider returned a different block height than requested")

    def _validate_request_after_recovery(
        self,
        checkpoint: IndexerCheckpoint,
        requested_start: BlockNumber,
    ) -> None:
        if requested_start.value > checkpoint.next_block_number.value:
            raise ReorganizationRangeError(
                "Reorganization recovery moved the checkpoint before the requested range; "
                f"rerun with START={checkpoint.next_block_number.value}"
            )

    async def _ensure_canonical_checkpoint(
        self,
        checkpoint: IndexerCheckpoint,
        recovery_budget: _ReorganizationRecoveryBudget,
    ) -> IndexerCheckpoint:
        current = checkpoint
        while True:
            canonical_tip = await self._get_canonical_checkpoint_tip(current)
            if canonical_tip.hash == current.expected_parent_hash:
                return current

            self._log_reorganization_detected(current, "canonical_tip_mismatch")
            cycle = recovery_budget.begin_cycle()
            try:
                local_snapshot = await self._snapshot_local_chain(current)
                ancestor = await self._discover_common_ancestor(
                    local_snapshot,
                    canonical_tip,
                )
                confirmed_ancestor = await self._get_rpc_block(
                    ancestor.number,
                    "confirm the reorganization ancestor",
                )
                if confirmed_ancestor.hash != ancestor.hash:
                    raise UnstableCanonicalChainError(
                        "The canonical ancestor changed before reorganization recovery"
                    )
            except UnstableCanonicalChainError:
                self._log_recovery_cycle(cycle, "provider_changed")
                continue

            try:
                current = await self._apply_rewind(local_snapshot, ancestor)
            except RewindCommitNotDurableError:
                self._log_recovery_cycle(cycle, "rewind_not_durable")
                durable = await self._inspect_checkpoint_state()
                if durable != current:
                    raise ReorganizationPlanStaleError(
                        "Checkpoint changed after a non-durable rewind attempt"
                    ) from None
                continue

            self._log_recovery_cycle(cycle, "rewound")
            canonical_after_rewind = await self._get_canonical_checkpoint_tip(current)
            if canonical_after_rewind.hash == current.expected_parent_hash:
                return current

    async def _get_canonical_checkpoint_tip(self, checkpoint: IndexerCheckpoint) -> Block:
        if checkpoint.next_block_number == checkpoint.start_block_number:
            if checkpoint.start_block_number.value == 0:
                return Block(
                    number=BlockNumber(0),
                    hash=_GENESIS_PARENT_HASH,
                    parent_hash=_GENESIS_PARENT_HASH,
                    timestamp=datetime.fromtimestamp(0, tz=UTC),
                )
            tip_number = BlockNumber(checkpoint.start_block_number.value - 1)
        else:
            tip_number = BlockNumber(checkpoint.next_block_number.value - 1)
        return await self._get_rpc_block(tip_number, "verify the canonical checkpoint tip")

    async def _get_rpc_block(self, number: BlockNumber, operation: str) -> Block:
        block = await self._rpc_read(
            operation,
            partial(self._blockchain.get_block, number),
        )
        if block.number != number:
            raise InvalidBlockchainResponseError(
                "Ethereum provider returned a different block height than requested"
            )
        return block

    async def _snapshot_local_chain(
        self,
        expected: IndexerCheckpoint,
    ) -> _LocalChainSnapshot:
        if expected.next_block_number == expected.start_block_number:
            raise ReorganizationStartBoundaryError(
                "Canonical history changed before the original indexing start boundary"
            )

        tip_number = expected.next_block_number.value - 1
        lowest_number = max(
            expected.start_block_number.value,
            tip_number - MAX_REORGANIZATION_DEPTH,
        )
        blocks: list[Block] = []
        async with self._unit_of_work_factory() as unit_of_work:
            durable = await unit_of_work.checkpoints.get_for_update()
            if durable != expected:
                raise ReorganizationPlanStaleError(
                    "Checkpoint changed before reorganization discovery"
                )
            highest = await unit_of_work.blocks.get_highest_number()
            if highest != BlockNumber(tip_number):
                raise IndexingConsistencyError(
                    "Persisted block tip changed before reorganization discovery"
                )

            higher: Block | None = None
            for height in range(tip_number, lowest_number - 1, -1):
                block = await unit_of_work.blocks.get_by_number(BlockNumber(height))
                if block is None:
                    raise IndexingConsistencyError(
                        "Persisted block history contains a missing height"
                    )
                if higher is not None and higher.parent_hash != block.hash:
                    raise IndexingConsistencyError(
                        "Persisted block history contains a broken parent link"
                    )
                blocks.append(block)
                higher = block

        return _LocalChainSnapshot(expected, tuple(blocks))

    async def _discover_common_ancestor(
        self,
        snapshot: _LocalChainSnapshot,
        canonical_tip: Block,
    ) -> Block:
        local_tip = snapshot.blocks_descending[0]
        if canonical_tip.number != local_tip.number:
            raise InvalidBlockchainResponseError(
                "Ethereum provider returned an incorrect canonical tip height"
            )
        if canonical_tip.hash == local_tip.hash:
            return local_tip

        canonical_higher = canonical_tip
        for local_block in snapshot.blocks_descending[1:]:
            canonical_block = await self._get_rpc_block(
                local_block.number,
                "discover a common canonical ancestor",
            )
            if canonical_higher.parent_hash != canonical_block.hash:
                raise UnstableCanonicalChainError(
                    "Ethereum provider returned an internally inconsistent block history"
                )
            if canonical_block.hash == local_block.hash:
                return local_block
            canonical_higher = canonical_block

        lowest = snapshot.blocks_descending[-1]
        if lowest.number == snapshot.checkpoint.start_block_number:
            raise ReorganizationStartBoundaryError(
                "No common ancestor exists at or above the original indexing start boundary"
            )
        raise ReorganizationDepthExceededError(
            "No common ancestor exists within the maximum 64-block rewind depth"
        )

    async def _apply_rewind(
        self,
        discovered: _LocalChainSnapshot,
        ancestor: Block,
    ) -> IndexerCheckpoint:
        expected = discovered.checkpoint
        intended = IndexerCheckpoint(
            chain_id=expected.chain_id,
            start_block_number=expected.start_block_number,
            next_block_number=BlockNumber(ancestor.number.value + 1),
            expected_parent_hash=ancestor.hash,
        )
        rewind_snapshot: _RewindSnapshot | None = None
        try:
            async with self._unit_of_work_factory() as unit_of_work:
                locked = await unit_of_work.checkpoints.get_for_update()
                if locked != expected:
                    raise ReorganizationPlanStaleError(
                        "Checkpoint changed during reorganization discovery"
                    )

                persisted_ancestor = await unit_of_work.blocks.get_by_number(ancestor.number)
                if persisted_ancestor != ancestor:
                    raise ReorganizationPlanStaleError(
                        "The discovered common ancestor changed before rewind"
                    )

                expected_descendants = tuple(
                    block
                    for block in reversed(discovered.blocks_descending)
                    if block.number.value > ancestor.number.value
                )
                transactions: list[tuple[BlockNumber, tuple[Transaction, ...]]] = []
                for block in expected_descendants:
                    persisted = await unit_of_work.blocks.get_by_number(block.number)
                    if persisted != block:
                        raise ReorganizationPlanStaleError(
                            "A reorganization descendant changed before rewind"
                        )
                    transactions.append(
                        (
                            block.number,
                            await unit_of_work.transactions.get_by_block_number(block.number),
                        )
                    )

                highest = await unit_of_work.blocks.get_highest_number()
                expected_tip = BlockNumber(expected.next_block_number.value - 1)
                if highest != expected_tip:
                    raise ReorganizationPlanStaleError("Persisted block tip changed before rewind")

                rewind_snapshot = _RewindSnapshot(
                    expected=expected,
                    intended=intended,
                    ancestor=ancestor,
                    descendants=expected_descendants,
                    descendant_transactions=tuple(transactions),
                )
                await unit_of_work.blocks.delete_after(ancestor.number)
                rewound = await unit_of_work.checkpoints.rewind(expected, ancestor)
                if rewound != intended:
                    raise IndexingConsistencyError(
                        "Checkpoint repository returned an unexpected rewind transition"
                    )
                await unit_of_work.commit()
                return rewound
        except CommitOutcomeUnknownError:
            assert rewind_snapshot is not None
            return await self._reconcile_rewind_commit(rewind_snapshot)

    async def _reconcile_rewind_commit(
        self,
        snapshot: _RewindSnapshot,
    ) -> IndexerCheckpoint:
        self._logger.warning(
            "commit_reconciliation_started",
            extra={"event": "commit_reconciliation_started", "operation": "checkpoint_rewind"},
        )
        async with self._unit_of_work_factory() as unit_of_work:
            durable = await unit_of_work.checkpoints.get_for_update()
            ancestor = await unit_of_work.blocks.get_by_number(snapshot.ancestor.number)
            highest = await unit_of_work.blocks.get_highest_number()

            if durable == snapshot.intended:
                descendants_absent = True
                for block in snapshot.descendants:
                    if await unit_of_work.blocks.get_by_number(block.number) is not None:
                        descendants_absent = False
                        break
                deleted_height_transactions_absent = True
                for number, _ in snapshot.descendant_transactions:
                    if await unit_of_work.transactions.get_by_block_number(number):
                        deleted_height_transactions_absent = False
                original_transaction_hashes_absent = True
                for _, transactions in snapshot.descendant_transactions:
                    for transaction in transactions:
                        if (
                            await unit_of_work.transactions.get_by_hash(transaction.hash)
                            is not None
                        ):
                            original_transaction_hashes_absent = False
                if (
                    ancestor == snapshot.ancestor
                    and highest == snapshot.ancestor.number
                    and descendants_absent
                    and deleted_height_transactions_absent
                    and original_transaction_hashes_absent
                ):
                    self._log_reconciliation("committed", "checkpoint_rewind")
                    return snapshot.intended
                self._log_reconciliation("inconsistent", "checkpoint_rewind")
                raise CommitReconciliationError(
                    "Checkpoint rewound but descendant deletion is incomplete"
                )

            if durable == snapshot.expected:
                original_tip = BlockNumber(snapshot.expected.next_block_number.value - 1)
                descendants_unchanged = True
                for block in snapshot.descendants:
                    if await unit_of_work.blocks.get_by_number(block.number) != block:
                        descendants_unchanged = False
                        break
                transactions_unchanged = True
                for number, transactions in snapshot.descendant_transactions:
                    if await unit_of_work.transactions.get_by_block_number(number) != transactions:
                        transactions_unchanged = False
                        break
                if (
                    ancestor == snapshot.ancestor
                    and highest == original_tip
                    and descendants_unchanged
                    and transactions_unchanged
                ):
                    self._log_reconciliation("not_durable", "checkpoint_rewind")
                    raise RewindCommitNotDurableError(
                        "Checkpoint rewind was not durable; canonical discovery must restart"
                    )

            self._log_reconciliation("inconsistent", "checkpoint_rewind")
            raise CommitReconciliationError("Checkpoint rewind commit left ambiguous durable state")

    def _log_reorganization_detected(
        self,
        checkpoint: IndexerCheckpoint,
        reason: str,
    ) -> None:
        self._logger.warning(
            "reorganization_detected",
            extra={
                "event": "reorganization_detected",
                "reason": reason,
                "next_block": checkpoint.next_block_number.value,
            },
        )

    def _log_recovery_cycle(self, cycle: int, outcome: str) -> None:
        self._logger.warning(
            "reorganization_recovery_cycle",
            extra={
                "event": "reorganization_recovery_cycle",
                "cycle": cycle,
                "max_cycles": MAX_REORGANIZATION_RECOVERY_CYCLES,
                "outcome": outcome,
            },
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
