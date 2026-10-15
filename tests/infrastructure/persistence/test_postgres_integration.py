"""PostgreSQL asyncpg integration tests.

These tests run against a real PostgreSQL instance when configured and reachable.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ethereum_wallet_intelligence.application import (
    BlockBundle,
    BlockchainResourceNotFoundError,
    BlockIndexer,
    BlockIndexingError,
    CheckpointConflictError,
    CommitNotDurableError,
    IndexerCheckpoint,
    IndexingConsistencyError,
    PersistenceConflictError,
    ReorganizationPlanStaleError,
    UnitOfWorkCleanupError,
    UnstableCanonicalChainError,
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
from ethereum_wallet_intelligence.infrastructure.persistence.database import create_session_factory
from ethereum_wallet_intelligence.infrastructure.persistence.mappers import transaction_to_model
from ethereum_wallet_intelligence.infrastructure.persistence.repositories import (
    SQLAlchemyBlockRepository,
    SQLAlchemyTransactionRepository,
)
from ethereum_wallet_intelligence.infrastructure.persistence.unit_of_work import (
    SQLAlchemyIndexerUnitOfWork,
)
from tests.support.postgres import prepare_test_database

pytestmark = pytest.mark.integration

BLOCK_HASH_1 = "0x" + "aa" * 32
BLOCK_HASH_2 = "0x" + "ab" * 32
PARENT_HASH_1 = "0x" + "bb" * 32
TX_HASH_1 = "0x" + "cc" * 32
TX_HASH_2 = "0x" + "cd" * 32
TX_HASH_3 = "0x" + "ce" * 32
SENDER_1 = "0x" + "11" * 20
RECIPIENT_1 = "0x" + "22" * 20
UINT256_MAX = 2**256 - 1


async def _get_postgres_engine(database_url: str) -> AsyncEngine:
    engine: AsyncEngine | None = None
    try:
        engine = create_async_engine(database_url)
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return engine
    except Exception:
        if engine is not None:
            await engine.dispose()
        raise RuntimeError("The dedicated PostgreSQL test database is not reachable") from None


async def _truncate_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE indexer_checkpoint, transactions, blocks RESTART IDENTITY CASCADE;"
            )
        )


def _run_with_postgres(test_fn: Callable[[AsyncEngine], Awaitable[None]]) -> None:
    database_url = prepare_test_database()

    async def _runner() -> None:
        engine = await _get_postgres_engine(database_url)
        try:
            await _truncate_tables(engine)
            await test_fn(engine)
        finally:
            try:
                await _truncate_tables(engine)
            finally:
                await engine.dispose()

    asyncio.run(_runner())


def test_postgres_block_and_tx_save_get_and_exact_uint256_wei() -> None:
    async def _test(engine: AsyncEngine) -> None:
        session_factory = create_session_factory(engine)

        block = Block(
            number=BlockNumber(21_000_000),
            hash=BlockHash(BLOCK_HASH_1),
            parent_hash=BlockHash(PARENT_HASH_1),
            timestamp=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        )

        tx = Transaction(
            hash=TransactionHash(TX_HASH_1),
            block_number=BlockNumber(21_000_000),
            sender=EthereumAddress(SENDER_1),
            to=EthereumAddress(RECIPIENT_1),
            value=Wei(UINT256_MAX),
            nonce=12,
            gas_limit=50_000,
            input_data=b"\xaa\xbb\xcc\xdd\xee\xff",
        )

        async with session_factory() as session, session.begin():
            block_repo = SQLAlchemyBlockRepository(session)
            await block_repo.save(block)
            tx_repo = SQLAlchemyTransactionRepository(session)
            await tx_repo.save(tx)

        async with session_factory() as session:
            block_repo = SQLAlchemyBlockRepository(session)
            tx_repo = SQLAlchemyTransactionRepository(session)

            saved_block = await block_repo.get_by_number(block.number)
            assert saved_block == block

            saved_tx = await tx_repo.get_by_hash(tx.hash)
            assert saved_tx == tx
            assert saved_tx is not None
            assert saved_tx.value.value == UINT256_MAX

    _run_with_postgres(_test)


def test_postgres_nullable_recipient_and_idempotency() -> None:
    async def _test(engine: AsyncEngine) -> None:
        session_factory = create_session_factory(engine)

        block = Block(
            number=BlockNumber(21_000_000),
            hash=BlockHash(BLOCK_HASH_1),
            parent_hash=BlockHash(PARENT_HASH_1),
            timestamp=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        )

        contract_creation_tx = Transaction(
            hash=TransactionHash(TX_HASH_2),
            block_number=BlockNumber(21_000_000),
            sender=EthereumAddress(SENDER_1),
            to=None,
            value=Wei(0),
            nonce=0,
            gas_limit=1_000_000,
            input_data=b"\x60\x80\x60\x40",
        )

        # First save
        async with session_factory() as session, session.begin():
            block_repo = SQLAlchemyBlockRepository(session)
            await block_repo.save(block)
            tx_repo = SQLAlchemyTransactionRepository(session)
            await tx_repo.save(contract_creation_tx)

        # Idempotent re-save
        async with session_factory() as session, session.begin():
            block_repo = SQLAlchemyBlockRepository(session)
            await block_repo.save(block)
            tx_repo = SQLAlchemyTransactionRepository(session)
            await tx_repo.save(contract_creation_tx)

        async with session_factory() as session:
            tx_repo = SQLAlchemyTransactionRepository(session)
            fetched = await tx_repo.get_by_hash(contract_creation_tx.hash)
            assert fetched == contract_creation_tx
            assert fetched is not None
            assert fetched.to is None

    _run_with_postgres(_test)


def test_postgres_duplicate_and_conflict_handling() -> None:
    async def _test(engine: AsyncEngine) -> None:
        session_factory = create_session_factory(engine)

        block = Block(
            number=BlockNumber(21_000_000),
            hash=BlockHash(BLOCK_HASH_1),
            parent_hash=BlockHash(PARENT_HASH_1),
            timestamp=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        )

        tx = Transaction(
            hash=TransactionHash(TX_HASH_1),
            block_number=BlockNumber(21_000_000),
            sender=EthereumAddress(SENDER_1),
            to=EthereumAddress(RECIPIENT_1),
            value=Wei(10**18),
            nonce=1,
            gas_limit=21_000,
            input_data=b"",
        )

        async with session_factory() as session, session.begin():
            block_repo = SQLAlchemyBlockRepository(session)
            await block_repo.save(block)
            tx_repo = SQLAlchemyTransactionRepository(session)
            await tx_repo.save(tx)

        conflicting_block = Block(
            number=BlockNumber(21_000_000),
            hash=BlockHash(BLOCK_HASH_2),
            parent_hash=BlockHash(PARENT_HASH_1),
            timestamp=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        )

        conflicting_tx = Transaction(
            hash=TransactionHash(TX_HASH_1),
            block_number=BlockNumber(21_000_000),
            sender=EthereumAddress(SENDER_1),
            to=EthereumAddress(RECIPIENT_1),
            value=Wei(999),
            nonce=1,
            gas_limit=21_000,
            input_data=b"",
        )

        async with session_factory() as session, session.begin():
            block_repo = SQLAlchemyBlockRepository(session)
            with pytest.raises(PersistenceConflictError, match="Conflicting block already exists"):
                await block_repo.save(conflicting_block)

        async with session_factory() as session, session.begin():
            tx_repo = SQLAlchemyTransactionRepository(session)
            with pytest.raises(
                PersistenceConflictError, match="Conflicting transaction already exists"
            ):
                await tx_repo.save(conflicting_tx)

    _run_with_postgres(_test)


def test_postgres_foreign_key_enforcement() -> None:
    async def _test(engine: AsyncEngine) -> None:
        session_factory = create_session_factory(engine)

        tx_missing_block = Transaction(
            hash=TransactionHash(TX_HASH_1),
            block_number=BlockNumber(99_999_999),
            sender=EthereumAddress(SENDER_1),
            to=EthereumAddress(RECIPIENT_1),
            value=Wei(10**18),
            nonce=1,
            gas_limit=21_000,
            input_data=b"",
        )

        async with session_factory() as session, session.begin():
            tx_repo = SQLAlchemyTransactionRepository(session)
            with pytest.raises(
                PersistenceConflictError, match="Referenced block 99999999 does not exist"
            ):
                await tx_repo.save(tx_missing_block)

        async with session_factory() as session:
            session.add(transaction_to_model(tx_missing_block))
            with pytest.raises(IntegrityError):
                await session.flush()
            await session.rollback()

    _run_with_postgres(_test)


def test_postgres_caller_owned_commit_and_rollback_boundaries() -> None:
    async def _test(engine: AsyncEngine) -> None:
        session_factory = create_session_factory(engine)

        committed_block = Block(
            number=BlockNumber(21_000_000),
            hash=BlockHash(BLOCK_HASH_1),
            parent_hash=BlockHash(PARENT_HASH_1),
            timestamp=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        )
        rolled_back_block = Block(
            number=BlockNumber(21_000_001),
            hash=BlockHash(BLOCK_HASH_2),
            parent_hash=BlockHash(BLOCK_HASH_1),
            timestamp=datetime(2026, 1, 1, 0, 0, 12, tzinfo=UTC),
        )
        rolled_back_tx = Transaction(
            hash=TransactionHash(TX_HASH_3),
            block_number=rolled_back_block.number,
            sender=EthereumAddress(SENDER_1),
            to=EthereumAddress(RECIPIENT_1),
            value=Wei(1),
            nonce=13,
            gas_limit=21_000,
            input_data=b"",
        )

        async with session_factory() as session, session.begin():
            await SQLAlchemyBlockRepository(session).save(committed_block)

        async with session_factory() as session:
            await SQLAlchemyBlockRepository(session).save(rolled_back_block)
            await SQLAlchemyTransactionRepository(session).save(rolled_back_tx)
            await session.rollback()

        async with session_factory() as session:
            block_repo = SQLAlchemyBlockRepository(session)
            tx_repo = SQLAlchemyTransactionRepository(session)
            assert await block_repo.get_by_number(committed_block.number) == committed_block
            assert await block_repo.get_by_number(rolled_back_block.number) is None
            assert await tx_repo.get_by_hash(rolled_back_tx.hash) is None

    _run_with_postgres(_test)


def _checkpoint(
    *,
    next_block: int = 21_000_000,
    expected_parent_hash: str = PARENT_HASH_1,
) -> IndexerCheckpoint:
    return IndexerCheckpoint(
        chain_id=ChainId.ethereum_mainnet(),
        start_block_number=BlockNumber(21_000_000),
        next_block_number=BlockNumber(next_block),
        expected_parent_hash=BlockHash(expected_parent_hash),
    )


def _block(number: int, block_hash: str, parent_hash: str) -> Block:
    return Block(
        number=BlockNumber(number),
        hash=BlockHash(block_hash),
        parent_hash=BlockHash(parent_hash),
        timestamp=datetime(2026, 1, 1, 0, 0, (number - 21_000_000) % 60, tzinfo=UTC),
    )


def _transaction(transaction_hash: str, block_number: int) -> Transaction:
    return Transaction(
        hash=TransactionHash(transaction_hash),
        block_number=BlockNumber(block_number),
        sender=EthereumAddress(SENDER_1),
        to=EthereumAddress(RECIPIENT_1),
        value=Wei(1),
        nonce=block_number,
        gas_limit=21_000,
        input_data=b"",
    )


def test_postgres_unit_of_work_atomically_commits_block_transactions_and_checkpoint() -> None:
    async def _test(engine: AsyncEngine) -> None:
        session_factory = create_session_factory(engine)
        initial = _checkpoint()
        block = _block(21_000_000, BLOCK_HASH_1, PARENT_HASH_1)
        transaction = _transaction(TX_HASH_1, block.number.value)

        async with SQLAlchemyIndexerUnitOfWork(session_factory) as unit_of_work:
            await unit_of_work.checkpoints.create(initial)
            await unit_of_work.blocks.save(block)
            await unit_of_work.transactions.save(transaction)
            advanced = await unit_of_work.checkpoints.advance(initial, block)
            await unit_of_work.commit()

        async with SQLAlchemyIndexerUnitOfWork(session_factory) as reader:
            assert await reader.blocks.get_by_number(block.number) == block
            assert await reader.transactions.get_by_hash(transaction.hash) == transaction
            assert await reader.checkpoints.get_for_update() == advanced

    _run_with_postgres(_test)


def test_postgres_unit_of_work_failure_and_missing_commit_leave_no_partial_state() -> None:
    async def _test(engine: AsyncEngine) -> None:
        session_factory = create_session_factory(engine)
        initial = _checkpoint()
        block = _block(21_000_000, BLOCK_HASH_1, PARENT_HASH_1)
        transaction = _transaction(TX_HASH_1, block.number.value)

        with pytest.raises(RuntimeError, match="injected failure"):
            async with SQLAlchemyIndexerUnitOfWork(session_factory) as unit_of_work:
                await unit_of_work.checkpoints.create(initial)
                await unit_of_work.blocks.save(block)
                await unit_of_work.transactions.save(transaction)
                await unit_of_work.checkpoints.advance(initial, block)
                raise RuntimeError("injected failure")

        async with SQLAlchemyIndexerUnitOfWork(session_factory) as reader:
            assert await reader.blocks.get_by_number(block.number) is None
            assert await reader.transactions.get_by_hash(transaction.hash) is None
            assert await reader.checkpoints.get_for_update() is None

        async with SQLAlchemyIndexerUnitOfWork(session_factory) as unit_of_work:
            await unit_of_work.checkpoints.create(initial)
            await unit_of_work.blocks.save(block)

        async with SQLAlchemyIndexerUnitOfWork(session_factory) as reader:
            assert await reader.blocks.get_by_number(block.number) is None
            assert await reader.checkpoints.get_for_update() is None

    _run_with_postgres(_test)


def test_postgres_concurrent_checkpoint_initialization_and_advance_reject_stale_writer() -> None:
    async def _test(engine: AsyncEngine) -> None:
        session_factory = create_session_factory(engine)
        initial = _checkpoint()

        first = SQLAlchemyIndexerUnitOfWork(session_factory)
        await first.__aenter__()
        try:
            await first.checkpoints.create(initial)
            contender_started = asyncio.Event()

            async def _create_contender() -> None:
                async with SQLAlchemyIndexerUnitOfWork(session_factory) as contender:
                    contender_started.set()
                    await contender.checkpoints.create(initial)
                    await contender.commit()

            contender_task = asyncio.create_task(_create_contender())
            await contender_started.wait()
            await asyncio.sleep(0)
            await first.commit()
        finally:
            await first.__aexit__(None, None, None)

        with pytest.raises(CheckpointConflictError):
            await contender_task

        block = _block(21_000_000, BLOCK_HASH_1, PARENT_HASH_1)
        async with SQLAlchemyIndexerUnitOfWork(session_factory) as setup:
            await setup.blocks.save(block)
            await setup.commit()

        first_writer = SQLAlchemyIndexerUnitOfWork(session_factory)
        await first_writer.__aenter__()
        try:
            assert await first_writer.checkpoints.get_for_update() == initial
            advanced = await first_writer.checkpoints.advance(initial, block)
            update_started = asyncio.Event()

            async def _update_contender() -> None:
                async with SQLAlchemyIndexerUnitOfWork(session_factory) as contender:
                    update_started.set()
                    await contender.checkpoints.advance(initial, block)
                    await contender.commit()

            update_task = asyncio.create_task(_update_contender())
            await update_started.wait()
            await asyncio.sleep(0)
            await first_writer.commit()
        finally:
            await first_writer.__aexit__(None, None, None)

        with pytest.raises(CheckpointConflictError, match="changed concurrently"):
            await update_task

        async with SQLAlchemyIndexerUnitOfWork(session_factory) as reader:
            assert await reader.checkpoints.get_for_update() == advanced

    _run_with_postgres(_test)


def test_postgres_rewind_and_on_delete_cascade_are_atomic() -> None:
    async def _test(engine: AsyncEngine) -> None:
        session_factory = create_session_factory(engine)
        initial = _checkpoint()
        block_1 = _block(21_000_000, BLOCK_HASH_1, PARENT_HASH_1)
        block_2 = _block(21_000_001, BLOCK_HASH_2, BLOCK_HASH_1)
        transaction_2 = _transaction(TX_HASH_2, block_2.number.value)

        async with SQLAlchemyIndexerUnitOfWork(session_factory) as setup:
            await setup.checkpoints.create(initial)
            await setup.blocks.save(block_1)
            after_1 = await setup.checkpoints.advance(initial, block_1)
            await setup.blocks.save(block_2)
            await setup.transactions.save(transaction_2)
            after_2 = await setup.checkpoints.advance(after_1, block_2)
            await setup.commit()

        async with SQLAlchemyIndexerUnitOfWork(session_factory) as rewind:
            assert await rewind.checkpoints.get_for_update() == after_2
            await rewind.blocks.delete_after(block_1.number)
            rewound = await rewind.checkpoints.rewind(after_2, block_1)
            await rewind.commit()

        async with SQLAlchemyIndexerUnitOfWork(session_factory) as reader:
            assert await reader.blocks.get_by_number(block_2.number) is None
            assert await reader.transactions.get_by_hash(transaction_2.hash) is None
            assert await reader.checkpoints.get_for_update() == rewound == after_1

    _run_with_postgres(_test)


def test_postgres_checkpoint_migration_constraints() -> None:
    async def _test(engine: AsyncEngine) -> None:
        insert_checkpoint = text(
            "INSERT INTO indexer_checkpoint "
            "(id, chain_id, start_block_number, next_block_number, expected_parent_hash) "
            "VALUES (:id, :chain_id, :start, :next, :block_hash)"
        )
        invalid_rows = (
            {"id": 2, "chain_id": 1, "start": 10, "next": 10},
            {"id": 1, "chain_id": 0, "start": 10, "next": 10},
            {"id": 1, "chain_id": 1, "start": -1, "next": 10},
            {"id": 1, "chain_id": 1, "start": 10, "next": 9},
        )
        for invalid_row in invalid_rows:
            async with engine.connect() as connection:
                transaction = await connection.begin()
                with pytest.raises(IntegrityError):
                    await connection.execute(
                        insert_checkpoint,
                        {**invalid_row, "block_hash": PARENT_HASH_1},
                    )
                await transaction.rollback()

    _run_with_postgres(_test)


class _IndexerGateway:
    def __init__(self) -> None:
        self.blocks: dict[BlockNumber, Block] = {}
        self.bundles: dict[BlockNumber, BlockBundle] = {}
        self.block_requests: list[BlockNumber] = []
        self.bundle_requests: list[BlockNumber] = []
        self.synchronize_bundle_reads = False
        self._bundle_read_count = 0
        self._bundle_read_barrier = asyncio.Event()
        self.block_barrier_limits: dict[BlockNumber, int] = {}
        self._block_read_counts: dict[BlockNumber, int] = {}
        self._block_read_barriers: dict[tuple[BlockNumber, int], asyncio.Event] = {}
        self.block_read_hook: Callable[[BlockNumber, int], Awaitable[None]] | None = None

    async def get_chain_id(self) -> ChainId:
        return ChainId.ethereum_mainnet()

    async def get_current_block_number(self) -> BlockNumber:
        raise AssertionError("current height is not used by BlockIndexer")

    async def get_block(self, number: BlockNumber) -> Block:
        self.block_requests.append(number)
        limit = self.block_barrier_limits.get(number, 0)
        count = self._block_read_counts.get(number, 0) + 1
        self._block_read_counts[number] = count
        if count <= limit:
            pair = (count - 1) // 2
            barrier = self._block_read_barriers.setdefault((number, pair), asyncio.Event())
            if count % 2 == 0:
                barrier.set()
            await barrier.wait()
        if self.block_read_hook is not None:
            await self.block_read_hook(number, count)
        block = self.blocks.get(number)
        if block is not None:
            return block
        bundle = self.bundles.get(number)
        if bundle is not None and bundle.block.number == number:
            return bundle.block
        following = self.bundles.get(BlockNumber(number.value + 1))
        if following is not None:
            return _block(number.value, str(following.block.parent_hash), PARENT_HASH_1)
        raise BlockchainResourceNotFoundError("test block was not found")

    async def get_block_with_transactions(self, number: BlockNumber) -> BlockBundle:
        self.bundle_requests.append(number)
        if self.synchronize_bundle_reads:
            self._bundle_read_count += 1
            if self._bundle_read_count == 2:
                self._bundle_read_barrier.set()
            await self._bundle_read_barrier.wait()
        try:
            return self.bundles[number]
        except KeyError as error:
            raise BlockchainResourceNotFoundError("test block bundle was not found") from error

    async def get_balance(self, address: EthereumAddress) -> Wei:
        raise AssertionError("balance is not used by BlockIndexer")

    async def get_transaction(self, transaction_hash: TransactionHash) -> Transaction:
        raise AssertionError("transaction lookup is not used by BlockIndexer")

    async def get_transaction_receipt(
        self,
        transaction_hash: TransactionHash,
    ) -> TransactionReceipt:
        raise AssertionError("receipt lookup is not used by BlockIndexer")


@dataclass(slots=True)
class _CommitFaultPlan:
    mode: str
    commit_calls: int = 0
    rollback_calls: int = 0
    faulted_session_id: int | None = None
    closed_session_ids: set[int] = field(default_factory=set)
    close_failure: BaseException | None = None
    invalidate_failure: BaseException | None = None
    invalidate_calls: int = 0


class _CommitFaultSession(AsyncSession):
    async def commit(self) -> None:
        plan = cast(_CommitFaultPlan, self.info["commit_fault_plan"])
        plan.commit_calls += 1
        mode = plan.mode
        if mode != "always_before_commit":
            plan.mode = "disabled"
        if mode in {"before_commit", "always_before_commit"}:
            plan.faulted_session_id = id(self)
            raise RuntimeError("database-secret-before-commit")

        await super().commit()
        if mode == "after_commit":
            plan.faulted_session_id = id(self)
            raise RuntimeError("database-secret-after-commit")

    async def rollback(self) -> None:
        plan = cast(_CommitFaultPlan, self.info["commit_fault_plan"])
        plan.rollback_calls += 1
        await super().rollback()

    async def close(self) -> None:
        plan = cast(_CommitFaultPlan, self.info["commit_fault_plan"])
        if plan.faulted_session_id == id(self) and plan.close_failure is not None:
            raise plan.close_failure
        await super().close()
        plan.closed_session_ids.add(id(self))

    async def invalidate(self) -> None:
        plan = cast(_CommitFaultPlan, self.info["commit_fault_plan"])
        plan.invalidate_calls += 1
        if plan.faulted_session_id == id(self) and plan.invalidate_failure is not None:
            raise plan.invalidate_failure
        await super().invalidate()


def _commit_fault_session_factory(
    engine: AsyncEngine,
    plan: _CommitFaultPlan,
) -> async_sessionmaker[AsyncSession]:
    factory = async_sessionmaker(
        bind=engine,
        class_=_CommitFaultSession,
        expire_on_commit=False,
        autoflush=False,
        info={"commit_fault_plan": plan},
    )
    return cast(async_sessionmaker[AsyncSession], factory)


def _reorg_hash(number: int, branch: int) -> str:
    return f"0x{branch:02x}{number:062x}"


def _reorg_chain(
    start: int,
    end: int,
    *,
    branch: int,
    parent_hash: str,
) -> list[Block]:
    blocks: list[Block] = []
    for number in range(start, end + 1):
        block = _block(number, _reorg_hash(number, branch), parent_hash)
        blocks.append(block)
        parent_hash = str(block.hash)
    return blocks


async def _persist_indexed_chain(
    session_factory: async_sessionmaker[AsyncSession],
    blocks: list[Block],
    transactions: tuple[Transaction, ...] = (),
) -> IndexerCheckpoint:
    checkpoint = IndexerCheckpoint(
        chain_id=ChainId.ethereum_mainnet(),
        start_block_number=blocks[0].number,
        next_block_number=BlockNumber(blocks[-1].number.value + 1),
        expected_parent_hash=blocks[-1].hash,
    )
    async with SQLAlchemyIndexerUnitOfWork(session_factory) as setup:
        for block in blocks:
            await setup.blocks.save(block)
        for transaction in transactions:
            await setup.transactions.save(transaction)
        await setup.checkpoints.create(checkpoint)
        await setup.commit()
    return checkpoint


def test_postgres_block_indexer_range_resume_and_atomic_persistence() -> None:
    async def _test(engine: AsyncEngine) -> None:
        session_factory = create_session_factory(engine)
        gateway = _IndexerGateway()
        anchor = _block(20_999_999, PARENT_HASH_1, "0x" + "01" * 32)
        block_1 = _block(21_000_000, BLOCK_HASH_1, PARENT_HASH_1)
        block_2 = _block(21_000_001, BLOCK_HASH_2, BLOCK_HASH_1)
        transaction_1 = _transaction(TX_HASH_1, block_2.number.value)
        transaction_2 = _transaction(TX_HASH_2, block_2.number.value)
        gateway.blocks[anchor.number] = anchor
        gateway.bundles[block_1.number] = BlockBundle(block_1, ())
        gateway.bundles[block_2.number] = BlockBundle(
            block_2,
            (transaction_1, transaction_2),
        )
        indexer = BlockIndexer(
            gateway,
            lambda: SQLAlchemyIndexerUnitOfWork(session_factory),
        )

        indexed = await indexer.index_range(block_1.number, block_2.number)
        already_complete = await indexer.index_range(block_1.number, block_2.number)

        assert indexed == 2
        assert already_complete == 0
        assert gateway.block_requests == [
            anchor.number,
            anchor.number,
            block_2.number,
            block_2.number,
        ]
        assert gateway.bundle_requests == [block_1.number, block_2.number]
        async with SQLAlchemyIndexerUnitOfWork(session_factory) as reader:
            assert await reader.blocks.get_by_number(block_1.number) == block_1
            assert await reader.blocks.get_by_number(block_2.number) == block_2
            assert await reader.transactions.get_by_hash(transaction_1.hash) == transaction_1
            assert await reader.transactions.get_by_hash(transaction_2.hash) == transaction_2
            checkpoint = await reader.checkpoints.get_for_update()
            assert checkpoint == IndexerCheckpoint(
                chain_id=ChainId.ethereum_mainnet(),
                start_block_number=block_1.number,
                next_block_number=BlockNumber(block_2.number.value + 1),
                expected_parent_hash=block_2.hash,
            )

    _run_with_postgres(_test)


def test_postgres_block_indexer_rejects_existing_data_without_checkpoint() -> None:
    async def _test(engine: AsyncEngine) -> None:
        session_factory = create_session_factory(engine)
        orphan = _block(21_000_000, BLOCK_HASH_1, PARENT_HASH_1)
        async with session_factory() as session, session.begin():
            await SQLAlchemyBlockRepository(session).save(orphan)

        gateway = _IndexerGateway()
        with pytest.raises(BlockIndexingError, match="without an indexing checkpoint"):
            await BlockIndexer(
                gateway,
                lambda: SQLAlchemyIndexerUnitOfWork(session_factory),
            ).index_range(orphan.number, orphan.number)

        assert gateway.block_requests == []
        assert gateway.bundle_requests == []

    _run_with_postgres(_test)


def test_postgres_block_indexer_rolls_back_intermediate_transaction_failure() -> None:
    async def _test(engine: AsyncEngine) -> None:
        session_factory = create_session_factory(engine)
        gateway = _IndexerGateway()
        anchor = _block(20_999_999, PARENT_HASH_1, "0x" + "01" * 32)
        block_1 = _block(21_000_000, BLOCK_HASH_1, PARENT_HASH_1)
        block_2 = _block(21_000_001, BLOCK_HASH_2, BLOCK_HASH_1)
        original = _transaction(TX_HASH_1, block_1.number.value)
        first_new = _transaction(TX_HASH_2, block_2.number.value)
        conflicting = _transaction(TX_HASH_1, block_2.number.value)
        gateway.blocks[anchor.number] = anchor
        gateway.bundles[block_1.number] = BlockBundle(block_1, (original,))
        gateway.bundles[block_2.number] = BlockBundle(block_2, (first_new, conflicting))
        indexer = BlockIndexer(
            gateway,
            lambda: SQLAlchemyIndexerUnitOfWork(session_factory),
        )

        assert await indexer.index_range(block_1.number, block_1.number) == 1
        with pytest.raises(PersistenceConflictError):
            await indexer.index_range(block_1.number, block_2.number)

        async with SQLAlchemyIndexerUnitOfWork(session_factory) as reader:
            assert await reader.blocks.get_by_number(block_2.number) is None
            assert await reader.transactions.get_by_hash(first_new.hash) is None
            assert await reader.transactions.get_by_hash(original.hash) == original
            checkpoint = await reader.checkpoints.get_for_update()
            assert checkpoint is not None
            assert checkpoint.next_block_number == block_2.number
            assert checkpoint.expected_parent_hash == block_1.hash

    _run_with_postgres(_test)


def test_postgres_block_indexer_concurrent_writers_reject_stale_bundle() -> None:
    async def _test(engine: AsyncEngine) -> None:
        session_factory = create_session_factory(engine)
        initial = _checkpoint()
        block = _block(21_000_000, BLOCK_HASH_1, PARENT_HASH_1)
        async with SQLAlchemyIndexerUnitOfWork(session_factory) as setup:
            await setup.checkpoints.create(initial)
            await setup.commit()

        gateway = _IndexerGateway()
        gateway.synchronize_bundle_reads = True
        gateway.bundles[block.number] = BlockBundle(block, ())
        first = BlockIndexer(
            gateway,
            lambda: SQLAlchemyIndexerUnitOfWork(session_factory),
        )
        second = BlockIndexer(
            gateway,
            lambda: SQLAlchemyIndexerUnitOfWork(session_factory),
        )

        outcomes = await asyncio.gather(
            first.index_range(block.number, block.number),
            second.index_range(block.number, block.number),
            return_exceptions=True,
        )

        assert sum(outcome == 1 for outcome in outcomes) == 1
        failures = [outcome for outcome in outcomes if isinstance(outcome, BlockIndexingError)]
        assert len(failures) == 1
        assert "changed during RPC retrieval" in str(failures[0])
        async with SQLAlchemyIndexerUnitOfWork(session_factory) as reader:
            assert await reader.blocks.get_by_number(block.number) == block
            checkpoint = await reader.checkpoints.get_for_update()
            assert checkpoint is not None
            assert checkpoint.next_block_number == BlockNumber(block.number.value + 1)

    _run_with_postgres(_test)


def test_postgres_block_indexer_reconciles_lost_commit_acknowledgement() -> None:
    async def _test(engine: AsyncEngine) -> None:
        session_factory = create_session_factory(engine)
        initial = _checkpoint()
        block = _block(21_000_000, BLOCK_HASH_1, PARENT_HASH_1)
        transaction = _transaction(TX_HASH_1, block.number.value)
        async with SQLAlchemyIndexerUnitOfWork(session_factory) as setup:
            await setup.checkpoints.create(initial)
            await setup.commit()

        gateway = _IndexerGateway()
        gateway.bundles[block.number] = BlockBundle(block, (transaction,))
        fault_plan = _CommitFaultPlan(mode="after_commit")
        fault_session_factory = _commit_fault_session_factory(engine, fault_plan)
        indexer = BlockIndexer(
            gateway,
            lambda: SQLAlchemyIndexerUnitOfWork(fault_session_factory),
        )

        assert await indexer.index_range(block.number, block.number) == 1
        assert fault_plan.commit_calls == 1
        assert fault_plan.faulted_session_id is not None
        assert fault_plan.faulted_session_id in fault_plan.closed_session_ids

        async with SQLAlchemyIndexerUnitOfWork(session_factory) as reader:
            assert await reader.blocks.get_by_number(block.number) == block
            assert await reader.transactions.get_by_hash(transaction.hash) == transaction
            checkpoint = await reader.checkpoints.get_for_update()
            assert checkpoint is not None
            assert checkpoint.next_block_number == BlockNumber(block.number.value + 1)
            assert checkpoint.expected_parent_hash == block.hash

    _run_with_postgres(_test)


def test_postgres_block_indexer_stops_when_uncertain_commit_was_not_durable() -> None:
    async def _test(engine: AsyncEngine) -> None:
        session_factory = create_session_factory(engine)
        initial = _checkpoint()
        block = _block(21_000_000, BLOCK_HASH_1, PARENT_HASH_1)
        transaction = _transaction(TX_HASH_1, block.number.value)
        async with SQLAlchemyIndexerUnitOfWork(session_factory) as setup:
            await setup.checkpoints.create(initial)
            await setup.commit()

        gateway = _IndexerGateway()
        gateway.bundles[block.number] = BlockBundle(block, (transaction,))
        fault_plan = _CommitFaultPlan(mode="before_commit")
        fault_session_factory = _commit_fault_session_factory(engine, fault_plan)
        indexer = BlockIndexer(
            gateway,
            lambda: SQLAlchemyIndexerUnitOfWork(fault_session_factory),
        )

        with pytest.raises(CommitNotDurableError, match="retry the same range"):
            await indexer.index_range(block.number, block.number)
        assert fault_plan.commit_calls == 1
        assert fault_plan.rollback_calls >= 1
        assert fault_plan.faulted_session_id is not None
        assert fault_plan.faulted_session_id in fault_plan.closed_session_ids

        async with SQLAlchemyIndexerUnitOfWork(session_factory) as reader:
            assert await reader.blocks.get_by_number(block.number) is None
            assert await reader.transactions.get_by_hash(transaction.hash) is None
            assert await reader.checkpoints.get_for_update() == initial

    _run_with_postgres(_test)


def test_postgres_startup_consistency_rejects_missing_checkpoint_tip() -> None:
    async def _test(engine: AsyncEngine) -> None:
        session_factory = create_session_factory(engine)
        checkpoint = _checkpoint(next_block=21_000_001, expected_parent_hash=BLOCK_HASH_1)
        async with SQLAlchemyIndexerUnitOfWork(session_factory) as setup:
            await setup.checkpoints.create(checkpoint)
            await setup.commit()

        with pytest.raises(IndexingConsistencyError, match=r"referenced.*missing"):
            await BlockIndexer(
                _IndexerGateway(),
                lambda: SQLAlchemyIndexerUnitOfWork(session_factory),
            ).index_range(BlockNumber(21_000_000), BlockNumber(21_000_000))

    _run_with_postgres(_test)


def test_postgres_startup_consistency_rejects_block_beyond_checkpoint() -> None:
    async def _test(engine: AsyncEngine) -> None:
        session_factory = create_session_factory(engine)
        tip = _block(21_000_000, BLOCK_HASH_1, PARENT_HASH_1)
        unexpected = _block(21_000_001, BLOCK_HASH_2, BLOCK_HASH_1)
        checkpoint = _checkpoint(next_block=21_000_001, expected_parent_hash=BLOCK_HASH_1)
        async with SQLAlchemyIndexerUnitOfWork(session_factory) as setup:
            await setup.checkpoints.create(checkpoint)
            await setup.blocks.save(tip)
            await setup.blocks.save(unexpected)
            await setup.commit()

        with pytest.raises(IndexingConsistencyError, match="beyond"):
            await BlockIndexer(
                _IndexerGateway(),
                lambda: SQLAlchemyIndexerUnitOfWork(session_factory),
            ).index_range(tip.number, tip.number)

    _run_with_postgres(_test)


def test_postgres_startup_consistency_rejects_checkpoint_tip_hash_mismatch() -> None:
    async def _test(engine: AsyncEngine) -> None:
        session_factory = create_session_factory(engine)
        tip = _block(21_000_000, BLOCK_HASH_1, PARENT_HASH_1)
        checkpoint = _checkpoint(next_block=21_000_001, expected_parent_hash=BLOCK_HASH_2)
        async with SQLAlchemyIndexerUnitOfWork(session_factory) as setup:
            await setup.checkpoints.create(checkpoint)
            await setup.blocks.save(tip)
            await setup.commit()

        with pytest.raises(IndexingConsistencyError, match="parent hash"):
            await BlockIndexer(
                _IndexerGateway(),
                lambda: SQLAlchemyIndexerUnitOfWork(session_factory),
            ).index_range(tip.number, tip.number)

    _run_with_postgres(_test)


def test_postgres_completed_range_reorg_atomically_deletes_descendants_and_transactions() -> None:
    async def _test(engine: AsyncEngine) -> None:
        session_factory = create_session_factory(engine)
        local = _reorg_chain(21_000_000, 21_000_002, branch=1, parent_hash=PARENT_HASH_1)
        transaction_1 = _transaction(TX_HASH_1, local[1].number.value)
        transaction_2 = _transaction(TX_HASH_2, local[2].number.value)
        await _persist_indexed_chain(
            session_factory,
            local,
            (transaction_1, transaction_2),
        )
        canonical = [
            local[0],
            *_reorg_chain(
                local[1].number.value,
                local[2].number.value,
                branch=2,
                parent_hash=str(local[0].hash),
            ),
        ]
        gateway = _IndexerGateway()
        gateway.blocks.update({block.number: block for block in canonical})

        indexed = await BlockIndexer(
            gateway,
            lambda: SQLAlchemyIndexerUnitOfWork(session_factory),
        ).index_range(local[0].number, local[0].number)

        assert indexed == 0
        async with SQLAlchemyIndexerUnitOfWork(session_factory) as reader:
            assert await reader.blocks.get_by_number(local[0].number) == local[0]
            assert await reader.blocks.get_by_number(local[1].number) is None
            assert await reader.blocks.get_by_number(local[2].number) is None
            assert await reader.transactions.get_by_hash(transaction_1.hash) is None
            assert await reader.transactions.get_by_hash(transaction_2.hash) is None
            checkpoint = await reader.checkpoints.get_for_update()
            assert checkpoint is not None
            assert checkpoint.next_block_number == local[1].number
            assert checkpoint.expected_parent_hash == local[0].hash

    _run_with_postgres(_test)


def test_postgres_rewind_reconciles_real_commit_acknowledgement_loss() -> None:
    async def _test(engine: AsyncEngine) -> None:
        session_factory = create_session_factory(engine)
        local = _reorg_chain(21_000_000, 21_000_001, branch=1, parent_hash=PARENT_HASH_1)
        transaction = _transaction(TX_HASH_1, local[1].number.value)
        await _persist_indexed_chain(session_factory, local, (transaction,))
        canonical_tip = _block(
            local[1].number.value,
            _reorg_hash(local[1].number.value, 2),
            str(local[0].hash),
        )
        gateway = _IndexerGateway()
        gateway.blocks.update(
            {
                local[0].number: local[0],
                canonical_tip.number: canonical_tip,
            }
        )
        fault_plan = _CommitFaultPlan(mode="after_commit")
        fault_factory = _commit_fault_session_factory(engine, fault_plan)

        indexed = await BlockIndexer(
            gateway,
            lambda: SQLAlchemyIndexerUnitOfWork(fault_factory),
        ).index_range(local[0].number, local[0].number)

        assert indexed == 0
        assert fault_plan.commit_calls == 1
        assert fault_plan.faulted_session_id in fault_plan.closed_session_ids
        async with SQLAlchemyIndexerUnitOfWork(session_factory) as reader:
            assert await reader.blocks.get_by_number(local[1].number) is None
            assert await reader.transactions.get_by_hash(transaction.hash) is None
            checkpoint = await reader.checkpoints.get_for_update()
            assert checkpoint is not None
            assert checkpoint.next_block_number == local[1].number

    _run_with_postgres(_test)


def test_postgres_rewind_before_commit_failures_roll_back_every_attempt() -> None:
    async def _test(engine: AsyncEngine) -> None:
        session_factory = create_session_factory(engine)
        local = _reorg_chain(21_000_000, 21_000_001, branch=1, parent_hash=PARENT_HASH_1)
        original = await _persist_indexed_chain(session_factory, local)
        canonical_tip = _block(
            local[1].number.value,
            _reorg_hash(local[1].number.value, 2),
            str(local[0].hash),
        )
        gateway = _IndexerGateway()
        gateway.blocks.update(
            {
                local[0].number: local[0],
                canonical_tip.number: canonical_tip,
            }
        )
        fault_plan = _CommitFaultPlan(mode="always_before_commit")
        fault_factory = _commit_fault_session_factory(engine, fault_plan)

        with pytest.raises(UnstableCanonicalChainError, match="3 recovery cycles"):
            await BlockIndexer(
                gateway,
                lambda: SQLAlchemyIndexerUnitOfWork(fault_factory),
            ).index_range(local[0].number, local[0].number)

        assert fault_plan.commit_calls == 3
        assert fault_plan.rollback_calls >= 3
        async with SQLAlchemyIndexerUnitOfWork(session_factory) as reader:
            assert await reader.blocks.get_by_number(local[1].number) == local[1]
            assert await reader.checkpoints.get_for_update() == original

    _run_with_postgres(_test)


def test_postgres_concurrent_recovery_rejects_stale_writer() -> None:
    async def _test(engine: AsyncEngine) -> None:
        session_factory = create_session_factory(engine)
        local = _reorg_chain(21_000_000, 21_000_001, branch=1, parent_hash=PARENT_HASH_1)
        await _persist_indexed_chain(session_factory, local)
        canonical_tip = _block(
            local[1].number.value,
            _reorg_hash(local[1].number.value, 2),
            str(local[0].hash),
        )
        gateway = _IndexerGateway()
        gateway.blocks.update(
            {
                local[0].number: local[0],
                canonical_tip.number: canonical_tip,
            }
        )
        gateway.block_barrier_limits = {
            local[1].number: 2,
            local[0].number: 4,
        }
        first = BlockIndexer(
            gateway,
            lambda: SQLAlchemyIndexerUnitOfWork(session_factory),
        )
        second = BlockIndexer(
            gateway,
            lambda: SQLAlchemyIndexerUnitOfWork(session_factory),
        )

        outcomes = await asyncio.gather(
            first.index_range(local[0].number, local[0].number),
            second.index_range(local[0].number, local[0].number),
            return_exceptions=True,
        )

        assert outcomes.count(0) == 1
        stale = [
            outcome for outcome in outcomes if isinstance(outcome, ReorganizationPlanStaleError)
        ]
        assert len(stale) == 1
        async with SQLAlchemyIndexerUnitOfWork(session_factory) as reader:
            assert await reader.blocks.get_by_number(local[1].number) is None
            checkpoint = await reader.checkpoints.get_for_update()
            assert checkpoint is not None
            assert checkpoint.next_block_number == local[1].number

    _run_with_postgres(_test)


@pytest.mark.parametrize("broken_history", ["missing", "mismatched"])
def test_postgres_recovery_rejects_invalid_local_ancestor_history(
    broken_history: str,
) -> None:
    async def _test(engine: AsyncEngine) -> None:
        session_factory = create_session_factory(engine)
        local = _reorg_chain(21_000_000, 21_000_002, branch=1, parent_hash=PARENT_HASH_1)
        if broken_history == "missing":
            persisted = [local[0], local[2]]
        else:
            persisted = [
                local[0],
                _block(local[1].number.value, str(local[1].hash), "0x" + "ff" * 32),
                local[2],
            ]
        checkpoint = IndexerCheckpoint(
            chain_id=ChainId.ethereum_mainnet(),
            start_block_number=local[0].number,
            next_block_number=BlockNumber(local[2].number.value + 1),
            expected_parent_hash=local[2].hash,
        )
        async with SQLAlchemyIndexerUnitOfWork(session_factory) as setup:
            for block in persisted:
                await setup.blocks.save(block)
            await setup.checkpoints.create(checkpoint)
            await setup.commit()
        canonical_tip = _block(
            local[2].number.value,
            _reorg_hash(local[2].number.value, 2),
            _reorg_hash(local[1].number.value, 2),
        )
        gateway = _IndexerGateway()
        gateway.blocks[canonical_tip.number] = canonical_tip

        expected_message = "missing height" if broken_history == "missing" else "broken parent link"
        with pytest.raises(IndexingConsistencyError, match=expected_message):
            await BlockIndexer(
                gateway,
                lambda: SQLAlchemyIndexerUnitOfWork(session_factory),
            ).index_range(local[0].number, local[2].number)

    _run_with_postgres(_test)


@pytest.mark.parametrize(
    ("statement_fragment", "expected_trigger"),
    [
        ("DELETE FROM BLOCKS", "descendant deletion"),
        ("UPDATE INDEXER_CHECKPOINT", "checkpoint rewind"),
    ],
)
def test_postgres_cancellation_during_atomic_rewind_rolls_back_every_change(
    statement_fragment: str,
    expected_trigger: str,
) -> None:
    async def _test(engine: AsyncEngine) -> None:
        session_factory = create_session_factory(engine)
        local = _reorg_chain(21_000_000, 21_000_001, branch=1, parent_hash=PARENT_HASH_1)
        transaction = _transaction(TX_HASH_1, local[1].number.value)
        original = await _persist_indexed_chain(session_factory, local, (transaction,))
        canonical_tip = _block(
            local[1].number.value,
            _reorg_hash(local[1].number.value, 2),
            str(local[0].hash),
        )
        gateway = _IndexerGateway()
        gateway.blocks.update(
            {
                local[0].number: local[0],
                canonical_tip.number: canonical_tip,
            }
        )
        cancellation_triggered = False

        def cancel_statement(
            connection: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            executemany: bool,
        ) -> None:
            nonlocal cancellation_triggered
            del connection, cursor, parameters, context, executemany
            if statement_fragment in statement.upper():
                cancellation_triggered = True
                raise asyncio.CancelledError(expected_trigger)

        event.listen(engine.sync_engine, "before_cursor_execute", cancel_statement)
        try:
            with pytest.raises(asyncio.CancelledError, match=expected_trigger):
                await BlockIndexer(
                    gateway,
                    lambda: SQLAlchemyIndexerUnitOfWork(session_factory),
                ).index_range(local[0].number, local[0].number)
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", cancel_statement)

        assert cancellation_triggered
        async with SQLAlchemyIndexerUnitOfWork(session_factory) as reader:
            assert await reader.blocks.get_by_number(local[1].number) == local[1]
            assert await reader.transactions.get_by_hash(transaction.hash) == transaction
            assert await reader.checkpoints.get_for_update() == original

    _run_with_postgres(_test)


def test_postgres_cancellation_during_ambiguous_rewind_cleanup_propagates() -> None:
    async def _test(engine: AsyncEngine) -> None:
        session_factory = create_session_factory(engine)
        local = _reorg_chain(21_000_000, 21_000_001, branch=1, parent_hash=PARENT_HASH_1)
        transaction = _transaction(TX_HASH_1, local[1].number.value)
        await _persist_indexed_chain(session_factory, local, (transaction,))
        canonical_tip = _block(
            local[1].number.value,
            _reorg_hash(local[1].number.value, 2),
            str(local[0].hash),
        )
        gateway = _IndexerGateway()
        gateway.blocks.update(
            {
                local[0].number: local[0],
                canonical_tip.number: canonical_tip,
            }
        )
        fault_plan = _CommitFaultPlan(
            mode="after_commit",
            close_failure=asyncio.CancelledError("cleanup cancelled"),
        )
        fault_factory = _commit_fault_session_factory(engine, fault_plan)

        with pytest.raises(asyncio.CancelledError, match="cleanup cancelled"):
            await BlockIndexer(
                gateway,
                lambda: SQLAlchemyIndexerUnitOfWork(fault_factory),
            ).index_range(local[0].number, local[0].number)

        assert fault_plan.commit_calls == 1
        assert fault_plan.invalidate_calls == 1
        async with SQLAlchemyIndexerUnitOfWork(session_factory) as reader:
            assert await reader.blocks.get_by_number(local[1].number) is None
            assert await reader.transactions.get_by_hash(transaction.hash) is None
            checkpoint = await reader.checkpoints.get_for_update()
            assert checkpoint is not None
            assert checkpoint.next_block_number == local[1].number

    _run_with_postgres(_test)


def test_postgres_cleanup_failure_blocks_rewind_reconciliation() -> None:
    async def _test(engine: AsyncEngine) -> None:
        session_factory = create_session_factory(engine)
        local = _reorg_chain(21_000_000, 21_000_001, branch=1, parent_hash=PARENT_HASH_1)
        await _persist_indexed_chain(session_factory, local)
        canonical_tip = _block(
            local[1].number.value,
            _reorg_hash(local[1].number.value, 2),
            str(local[0].hash),
        )
        gateway = _IndexerGateway()
        gateway.blocks.update(
            {
                local[0].number: local[0],
                canonical_tip.number: canonical_tip,
            }
        )
        fault_plan = _CommitFaultPlan(
            mode="after_commit",
            close_failure=RuntimeError("database-secret-close"),
            invalidate_failure=RuntimeError("database-secret-invalidate"),
        )
        fault_factory = _commit_fault_session_factory(engine, fault_plan)

        with pytest.raises(UnitOfWorkCleanupError, match="cleanup could not be confirmed"):
            await BlockIndexer(
                gateway,
                lambda: SQLAlchemyIndexerUnitOfWork(fault_factory),
            ).index_range(local[0].number, local[0].number)

        assert fault_plan.commit_calls == 1
        assert fault_plan.invalidate_calls == 1
        async with SQLAlchemyIndexerUnitOfWork(session_factory) as reader:
            assert await reader.blocks.get_by_number(local[1].number) is None
            checkpoint = await reader.checkpoints.get_for_update()
            assert checkpoint is not None
            assert checkpoint.next_block_number == local[1].number

    _run_with_postgres(_test)


def test_postgres_concurrent_ancestor_change_without_checkpoint_advance_is_stale() -> None:
    async def _test(engine: AsyncEngine) -> None:
        session_factory = create_session_factory(engine)
        local = _reorg_chain(21_000_000, 21_000_001, branch=1, parent_hash=PARENT_HASH_1)
        original = await _persist_indexed_chain(session_factory, local)
        canonical_tip = _block(
            local[1].number.value,
            _reorg_hash(local[1].number.value, 2),
            str(local[0].hash),
        )
        gateway = _IndexerGateway()
        gateway.blocks.update(
            {
                local[0].number: local[0],
                canonical_tip.number: canonical_tip,
            }
        )

        async def change_ancestor_after_confirmation(number: BlockNumber, read_count: int) -> None:
            if number != local[0].number or read_count != 2:
                return
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE blocks SET timestamp = timestamp + INTERVAL '1 second' "
                        "WHERE number = :number"
                    ),
                    {"number": number.value},
                )

        gateway.block_read_hook = change_ancestor_after_confirmation

        with pytest.raises(ReorganizationPlanStaleError, match="ancestor changed"):
            await BlockIndexer(
                gateway,
                lambda: SQLAlchemyIndexerUnitOfWork(session_factory),
            ).index_range(local[0].number, local[0].number)

        async with SQLAlchemyIndexerUnitOfWork(session_factory) as reader:
            assert await reader.blocks.get_by_number(local[0].number) != local[0]
            assert await reader.blocks.get_by_number(local[1].number) == local[1]
            assert await reader.checkpoints.get_for_update() == original

    _run_with_postgres(_test)
