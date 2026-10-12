"""Deterministic unit tests for the bounded sequential block indexer."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Self

import pytest

from ethereum_wallet_intelligence.application import (
    BlockBundle,
    BlockchainProviderError,
    BlockIndexer,
    BlockIndexingError,
    CheckpointConflictError,
    IndexerCheckpoint,
    InvalidBlockRangeError,
)
from ethereum_wallet_intelligence.domain.ethereum import (
    Block,
    BlockHash,
    BlockNumber,
    ChainId,
    EthereumAddress,
    Transaction,
    TransactionHash,
    TransactionReceipt,
    Wei,
)

GENESIS_PARENT_HASH = "0x" + "00" * 32
HASH_9 = "0x" + "09" * 32
HASH_10 = "0x" + "10" * 32
HASH_11 = "0x" + "11" * 32
HASH_12 = "0x" + "12" * 32
WRONG_HASH = "0x" + "ff" * 32
SENDER = "0x" + "aa" * 20
RECIPIENT = "0x" + "bb" * 20


def _block(number: int, block_hash: str, parent_hash: str) -> Block:
    return Block(
        number=BlockNumber(number),
        hash=BlockHash(block_hash),
        parent_hash=BlockHash(parent_hash),
        timestamp=datetime(2026, 1, 1, 0, 0, number % 60, tzinfo=UTC),
    )


def _transaction(number: int, suffix: str, nonce: int = 0) -> Transaction:
    return Transaction(
        hash=TransactionHash("0x" + suffix * 32),
        block_number=BlockNumber(number),
        sender=EthereumAddress(SENDER),
        to=EthereumAddress(RECIPIENT),
        value=Wei(nonce),
        nonce=nonce,
        gas_limit=21_000,
        input_data=b"",
    )


@dataclass(slots=True)
class _MemoryState:
    blocks: dict[BlockNumber, Block] = field(default_factory=dict)
    transactions: dict[TransactionHash, Transaction] = field(default_factory=dict)
    checkpoint: IndexerCheckpoint | None = None


@dataclass(slots=True)
class _FailurePlan:
    fail_on_transaction_save: int | None = None
    failure: BaseException | None = None
    transaction_save_calls: int = 0


class _MemoryBlockRepository:
    def __init__(self, state: _MemoryState) -> None:
        self._state = state

    async def get_by_number(self, block_number: BlockNumber) -> Block | None:
        return self._state.blocks.get(block_number)

    async def has_any(self) -> bool:
        return bool(self._state.blocks)

    async def save(self, block: Block) -> None:
        self._state.blocks[block.number] = block

    async def delete_after(self, block_number: BlockNumber) -> None:
        self._state.blocks = {
            number: block
            for number, block in self._state.blocks.items()
            if number.value <= block_number.value
        }


class _MemoryTransactionRepository:
    def __init__(self, state: _MemoryState, failure_plan: _FailurePlan) -> None:
        self._state = state
        self._failure_plan = failure_plan

    async def get_by_hash(self, transaction_hash: TransactionHash) -> Transaction | None:
        return self._state.transactions.get(transaction_hash)

    async def has_any(self) -> bool:
        return bool(self._state.transactions)

    async def save(self, transaction: Transaction) -> None:
        self._failure_plan.transaction_save_calls += 1
        if self._failure_plan.fail_on_transaction_save == self._failure_plan.transaction_save_calls:
            assert self._failure_plan.failure is not None
            raise self._failure_plan.failure
        self._state.transactions[transaction.hash] = transaction


class _MemoryCheckpointRepository:
    def __init__(self, state: _MemoryState) -> None:
        self._state = state

    async def get_for_update(self) -> IndexerCheckpoint | None:
        return self._state.checkpoint

    async def create(self, checkpoint: IndexerCheckpoint) -> None:
        if self._state.checkpoint is not None:
            raise CheckpointConflictError("checkpoint already exists")
        self._state.checkpoint = checkpoint

    async def advance(
        self,
        expected: IndexerCheckpoint,
        block: Block,
    ) -> IndexerCheckpoint:
        if self._state.checkpoint != expected:
            raise CheckpointConflictError("stale checkpoint")
        advanced = IndexerCheckpoint(
            chain_id=expected.chain_id,
            start_block_number=expected.start_block_number,
            next_block_number=BlockNumber(block.number.value + 1),
            expected_parent_hash=block.hash,
        )
        self._state.checkpoint = advanced
        return advanced

    async def rewind(
        self,
        expected: IndexerCheckpoint,
        ancestor: Block,
    ) -> IndexerCheckpoint:
        raise AssertionError("rewind is not used by BlockIndexer")


class _MemoryUnitOfWork:
    def __init__(self, durable: _MemoryState, failure_plan: _FailurePlan) -> None:
        self._durable = durable
        self._failure_plan = failure_plan
        self._working: _MemoryState | None = None
        self._blocks: _MemoryBlockRepository | None = None
        self._transactions: _MemoryTransactionRepository | None = None
        self._checkpoints: _MemoryCheckpointRepository | None = None

    @property
    def blocks(self) -> _MemoryBlockRepository:
        assert self._blocks is not None
        return self._blocks

    @property
    def transactions(self) -> _MemoryTransactionRepository:
        assert self._transactions is not None
        return self._transactions

    @property
    def checkpoints(self) -> _MemoryCheckpointRepository:
        assert self._checkpoints is not None
        return self._checkpoints

    async def __aenter__(self) -> Self:
        working = _MemoryState(
            blocks=dict(self._durable.blocks),
            transactions=dict(self._durable.transactions),
            checkpoint=self._durable.checkpoint,
        )
        self._working = working
        self._blocks = _MemoryBlockRepository(working)
        self._transactions = _MemoryTransactionRepository(working, self._failure_plan)
        self._checkpoints = _MemoryCheckpointRepository(working)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self._working = None
        self._blocks = None
        self._transactions = None
        self._checkpoints = None

    async def commit(self) -> None:
        assert self._working is not None
        self._durable.blocks = dict(self._working.blocks)
        self._durable.transactions = dict(self._working.transactions)
        self._durable.checkpoint = self._working.checkpoint


class _MemoryUnitOfWorkFactory:
    def __init__(
        self,
        state: _MemoryState | None = None,
        failure_plan: _FailurePlan | None = None,
    ) -> None:
        self.state = state or _MemoryState()
        self.failure_plan = failure_plan or _FailurePlan()

    def __call__(self) -> _MemoryUnitOfWork:
        return _MemoryUnitOfWork(self.state, self.failure_plan)


class _FakeBlockchainGateway:
    def __init__(self, chain_id: int = 1) -> None:
        self.chain_id = ChainId(chain_id)
        self.blocks: dict[BlockNumber, Block] = {}
        self.bundles: dict[BlockNumber, BlockBundle] = {}
        self.block_requests: list[BlockNumber] = []
        self.bundle_requests: list[BlockNumber] = []
        self.bundle_failure: BaseException | None = None
        self.block_hook: Callable[[BlockNumber], None] | None = None
        self.bundle_hook: Callable[[BlockNumber], None] | None = None

    async def get_chain_id(self) -> ChainId:
        return self.chain_id

    async def get_current_block_number(self) -> BlockNumber:
        raise AssertionError("current block lookup is not used by BlockIndexer")

    async def get_block(self, number: BlockNumber) -> Block:
        self.block_requests.append(number)
        if self.block_hook is not None:
            self.block_hook(number)
        return self.blocks[number]

    async def get_block_with_transactions(self, number: BlockNumber) -> BlockBundle:
        self.bundle_requests.append(number)
        if self.bundle_failure is not None:
            raise self.bundle_failure
        bundle = self.bundles[number]
        if self.bundle_hook is not None:
            self.bundle_hook(number)
        return bundle

    async def get_balance(self, address: EthereumAddress) -> Wei:
        raise AssertionError("balance lookup is not used by BlockIndexer")

    async def get_transaction(self, transaction_hash: TransactionHash) -> Transaction:
        raise AssertionError("transaction lookup is not used by BlockIndexer")

    async def get_transaction_receipt(
        self,
        transaction_hash: TransactionHash,
    ) -> TransactionReceipt:
        raise AssertionError("receipt lookup is not used by BlockIndexer")


def _checkpoint(
    *,
    chain_id: int = 1,
    start: int = 10,
    next_block: int = 10,
    parent_hash: str = HASH_9,
) -> IndexerCheckpoint:
    return IndexerCheckpoint(
        chain_id=ChainId(chain_id),
        start_block_number=BlockNumber(start),
        next_block_number=BlockNumber(next_block),
        expected_parent_hash=BlockHash(parent_hash),
    )


def _add_linear_blocks(
    gateway: _FakeBlockchainGateway,
    start: int,
    hashes: list[str],
) -> list[Block]:
    parent_hash = GENESIS_PARENT_HASH if start == 0 else f"0x{start - 1:064x}"
    if start > 0:
        gateway.blocks[BlockNumber(start - 1)] = _block(start - 1, parent_hash, WRONG_HASH)

    blocks: list[Block] = []
    for offset, block_hash in enumerate(hashes):
        number = start + offset
        block = _block(number, block_hash, parent_hash)
        gateway.bundles[block.number] = BlockBundle(block=block, transactions=())
        blocks.append(block)
        parent_hash = block_hash
    return blocks


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        (10, 9, "cannot precede"),
        (0, 100, "cannot exceed 100"),
    ],
)
def test_index_range_rejects_invalid_or_excessive_ranges(
    start: int,
    end: int,
    message: str,
) -> None:
    indexer = BlockIndexer(_FakeBlockchainGateway(), _MemoryUnitOfWorkFactory())

    with pytest.raises(InvalidBlockRangeError, match=message):
        asyncio.run(indexer.index_range(BlockNumber(start), BlockNumber(end)))


def test_index_range_accepts_exactly_one_hundred_blocks() -> None:
    gateway = _FakeBlockchainGateway()
    hashes = [f"0x{number:064x}" for number in range(1, 101)]
    _add_linear_blocks(gateway, 1, hashes)
    factory = _MemoryUnitOfWorkFactory()

    indexed = asyncio.run(
        BlockIndexer(gateway, factory).index_range(BlockNumber(1), BlockNumber(100))
    )

    assert indexed == 100
    assert factory.state.checkpoint == _checkpoint(
        start=1,
        next_block=101,
        parent_hash=hashes[-1],
    )


def test_fresh_checkpoint_indexes_inclusive_empty_and_multi_transaction_blocks() -> None:
    gateway = _FakeBlockchainGateway()
    blocks = _add_linear_blocks(gateway, 10, [HASH_10, HASH_11])
    transactions = (_transaction(11, "a1"), _transaction(11, "a2", nonce=1))
    gateway.bundles[BlockNumber(11)] = BlockBundle(
        block=blocks[1],
        transactions=transactions,
    )
    factory = _MemoryUnitOfWorkFactory()

    indexed = asyncio.run(
        BlockIndexer(gateway, factory).index_range(BlockNumber(10), BlockNumber(11))
    )

    assert indexed == 2
    assert gateway.block_requests == [BlockNumber(9)]
    assert gateway.bundle_requests == [BlockNumber(10), BlockNumber(11)]
    assert factory.state.blocks == {block.number: block for block in blocks}
    assert tuple(factory.state.transactions.values()) == transactions
    assert factory.state.checkpoint == _checkpoint(next_block=12, parent_hash=HASH_11)


def test_genesis_initialization_uses_zero_parent_hash_without_anchor_request() -> None:
    gateway = _FakeBlockchainGateway()
    blocks = _add_linear_blocks(gateway, 0, [HASH_10])
    factory = _MemoryUnitOfWorkFactory()

    indexed = asyncio.run(
        BlockIndexer(gateway, factory).index_range(BlockNumber(0), BlockNumber(0))
    )

    assert indexed == 1
    assert gateway.block_requests == []
    assert factory.state.blocks == {BlockNumber(0): blocks[0]}


def test_checkpoint_initialization_adopts_safe_concurrent_winner() -> None:
    state = _MemoryState()
    factory = _MemoryUnitOfWorkFactory(state)
    gateway = _FakeBlockchainGateway()
    anchor = _block(9, HASH_9, WRONG_HASH)
    block = _block(10, HASH_10, HASH_9)
    gateway.blocks[anchor.number] = anchor
    gateway.bundles[block.number] = BlockBundle(block, ())

    def initialize_competing_writer(_: BlockNumber) -> None:
        state.checkpoint = _checkpoint()

    gateway.block_hook = initialize_competing_writer

    indexed = asyncio.run(
        BlockIndexer(gateway, factory).index_range(BlockNumber(10), BlockNumber(10))
    )

    assert indexed == 1
    assert state.blocks == {block.number: block}
    assert state.checkpoint == _checkpoint(next_block=11, parent_hash=HASH_10)


def test_incorrect_anchor_or_bundle_height_is_rejected() -> None:
    gateway = _FakeBlockchainGateway()
    gateway.blocks[BlockNumber(9)] = _block(8, HASH_9, WRONG_HASH)
    with pytest.raises(BlockIndexingError, match="incorrect checkpoint anchor height"):
        asyncio.run(
            BlockIndexer(gateway, _MemoryUnitOfWorkFactory()).index_range(
                BlockNumber(10), BlockNumber(10)
            )
        )

    state = _MemoryState(checkpoint=_checkpoint())
    gateway = _FakeBlockchainGateway()
    wrong_height = _block(11, HASH_11, HASH_9)
    gateway.bundles[BlockNumber(10)] = BlockBundle(wrong_height, ())
    with pytest.raises(BlockIndexingError, match="different block height"):
        asyncio.run(
            BlockIndexer(gateway, _MemoryUnitOfWorkFactory(state)).index_range(
                BlockNumber(10), BlockNumber(10)
            )
        )


def test_provider_and_checkpoint_chain_mismatches_are_rejected() -> None:
    wrong_provider = _FakeBlockchainGateway(chain_id=8453)
    with pytest.raises(BlockIndexingError, match="Mainnet only"):
        asyncio.run(
            BlockIndexer(wrong_provider, _MemoryUnitOfWorkFactory()).index_range(
                BlockNumber(10), BlockNumber(10)
            )
        )

    state = _MemoryState(checkpoint=_checkpoint(chain_id=8453))
    with pytest.raises(BlockIndexingError, match="chain IDs do not match"):
        asyncio.run(
            BlockIndexer(_FakeBlockchainGateway(), _MemoryUnitOfWorkFactory(state)).index_range(
                BlockNumber(10), BlockNumber(10)
            )
        )


def test_indexer_resumes_after_intermediate_persistence_failure() -> None:
    gateway = _FakeBlockchainGateway()
    blocks = _add_linear_blocks(gateway, 10, [HASH_10, HASH_11, HASH_12])
    transactions = (_transaction(11, "b1"), _transaction(11, "b2", nonce=1))
    gateway.bundles[BlockNumber(11)] = BlockBundle(block=blocks[1], transactions=transactions)
    failure_plan = _FailurePlan(
        fail_on_transaction_save=2,
        failure=RuntimeError("injected persistence failure"),
    )
    factory = _MemoryUnitOfWorkFactory(failure_plan=failure_plan)
    indexer = BlockIndexer(gateway, factory)

    with pytest.raises(RuntimeError, match="injected persistence failure"):
        asyncio.run(indexer.index_range(BlockNumber(10), BlockNumber(12)))

    assert factory.state.checkpoint == _checkpoint(next_block=11, parent_hash=HASH_10)
    assert set(factory.state.blocks) == {BlockNumber(10)}
    assert factory.state.transactions == {}

    failure_plan.fail_on_transaction_save = None
    indexed = asyncio.run(indexer.index_range(BlockNumber(10), BlockNumber(12)))

    assert indexed == 2
    assert factory.state.checkpoint == _checkpoint(next_block=13, parent_hash=HASH_12)
    assert set(factory.state.blocks) == {BlockNumber(10), BlockNumber(11), BlockNumber(12)}


def test_existing_data_without_checkpoint_is_rejected_before_anchor_rpc() -> None:
    orphan = _block(10, HASH_10, HASH_9)
    state = _MemoryState(blocks={orphan.number: orphan})
    gateway = _FakeBlockchainGateway()

    with pytest.raises(BlockIndexingError, match="without an indexing checkpoint"):
        asyncio.run(
            BlockIndexer(gateway, _MemoryUnitOfWorkFactory(state)).index_range(
                BlockNumber(10), BlockNumber(10)
            )
        )

    assert gateway.block_requests == []
    assert gateway.bundle_requests == []


def test_resume_rejects_skips_and_treats_completed_range_as_no_op() -> None:
    state = _MemoryState(checkpoint=_checkpoint(next_block=12, parent_hash=HASH_11))
    factory = _MemoryUnitOfWorkFactory(state)
    gateway = _FakeBlockchainGateway()
    indexer = BlockIndexer(gateway, factory)

    with pytest.raises(BlockIndexingError, match="skip unindexed"):
        asyncio.run(indexer.index_range(BlockNumber(13), BlockNumber(13)))

    indexed = asyncio.run(indexer.index_range(BlockNumber(10), BlockNumber(11)))
    assert indexed == 0
    assert gateway.bundle_requests == []


def test_stale_checkpoint_after_rpc_is_rejected_without_bundle_commit() -> None:
    state = _MemoryState(checkpoint=_checkpoint())
    factory = _MemoryUnitOfWorkFactory(state)
    gateway = _FakeBlockchainGateway()
    block = _block(10, HASH_10, HASH_9)
    gateway.bundles[block.number] = BlockBundle(block=block, transactions=())

    def advance_competing_writer(_: BlockNumber) -> None:
        state.blocks[block.number] = block
        state.checkpoint = _checkpoint(next_block=11, parent_hash=HASH_10)

    gateway.bundle_hook = advance_competing_writer

    with pytest.raises(BlockIndexingError, match="changed during RPC retrieval"):
        asyncio.run(BlockIndexer(gateway, factory).index_range(BlockNumber(10), BlockNumber(10)))

    assert state.checkpoint == _checkpoint(next_block=11, parent_hash=HASH_10)
    assert state.blocks == {block.number: block}


def test_wrong_parent_hash_halts_without_persistence() -> None:
    canonical = _block(10, HASH_10, HASH_9)
    state = _MemoryState(
        blocks={canonical.number: canonical},
        checkpoint=_checkpoint(next_block=11, parent_hash=HASH_10),
    )
    gateway = _FakeBlockchainGateway()
    wrong = _block(11, HASH_11, WRONG_HASH)
    gateway.bundles[wrong.number] = BlockBundle(block=wrong, transactions=())

    with pytest.raises(BlockIndexingError, match="possible reorganization"):
        asyncio.run(
            BlockIndexer(gateway, _MemoryUnitOfWorkFactory(state)).index_range(
                BlockNumber(10), BlockNumber(11)
            )
        )

    assert state.blocks == {canonical.number: canonical}
    assert state.checkpoint == _checkpoint(next_block=11, parent_hash=HASH_10)


def test_rpc_failure_does_not_advance_checkpoint() -> None:
    state = _MemoryState(checkpoint=_checkpoint())
    gateway = _FakeBlockchainGateway()
    gateway.bundle_failure = BlockchainProviderError("provider unavailable")

    with pytest.raises(BlockchainProviderError, match="provider unavailable"):
        asyncio.run(
            BlockIndexer(gateway, _MemoryUnitOfWorkFactory(state)).index_range(
                BlockNumber(10), BlockNumber(10)
            )
        )

    assert state.blocks == {}
    assert state.checkpoint == _checkpoint()


def test_cancellation_during_transaction_save_rolls_back_bundle_and_checkpoint() -> None:
    state = _MemoryState(checkpoint=_checkpoint())
    gateway = _FakeBlockchainGateway()
    block = _block(10, HASH_10, HASH_9)
    gateway.bundles[block.number] = BlockBundle(
        block=block,
        transactions=(_transaction(10, "c1"),),
    )
    failure_plan = _FailurePlan(
        fail_on_transaction_save=1,
        failure=asyncio.CancelledError(),
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            BlockIndexer(
                gateway,
                _MemoryUnitOfWorkFactory(state, failure_plan),
            ).index_range(BlockNumber(10), BlockNumber(10))
        )

    assert state.blocks == {}
    assert state.transactions == {}
    assert state.checkpoint == _checkpoint()
