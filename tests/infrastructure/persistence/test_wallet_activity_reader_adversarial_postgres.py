"""Adversarial PostgreSQL snapshot tests for the wallet activity reader."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from pydantic_settings import SettingsConfigDict
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from ethereum_wallet_intelligence.application import (
    IndexerCheckpoint,
    StaleWalletActivityCursorError,
    WalletActivityPage,
    WalletActivityQuery,
)
from ethereum_wallet_intelligence.domain.ethereum import (
    Block,
    BlockHash,
    BlockNumber,
    ChainId,
    EthereumAddress,
    Transaction,
    TransactionHash,
    Wei,
)
from ethereum_wallet_intelligence.infrastructure.persistence.database import (
    create_session_factory,
)
from ethereum_wallet_intelligence.infrastructure.persistence.models import TransactionModel
from ethereum_wallet_intelligence.infrastructure.persistence.unit_of_work import (
    SQLAlchemyIndexerUnitOfWork,
)
from ethereum_wallet_intelligence.infrastructure.persistence.wallet_activity_reader import (
    SQLAlchemyWalletActivityReader,
    _SnapshotCoverage,
)
from tests.support.postgres import PostgreSQLTestSettings, prepare_test_database

pytestmark = pytest.mark.integration

WALLET = EthereumAddress("0x" + "11" * 20)
OTHER = EthereumAddress("0x" + "22" * 20)
START = 10
_WAIT_TIMEOUT_SECONDS = 5.0


class _EnvironmentOnlyPostgreSQLTestSettings(PostgreSQLTestSettings):
    """Load explicit process environment variables without consulting dotenv."""

    model_config = SettingsConfigDict(
        env_file=None,
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
        populate_by_name=True,
    )


def _block_hash(number: int, *, branch: int = 0) -> str:
    return f"0x{branch:02x}{number:062x}"


def _transaction_hash(number: int, *, branch: int = 0) -> str:
    return f"0x{branch:02x}{number:062x}"


def _block(number: int, *, branch: int = 0, parent_hash: str | None = None) -> Block:
    return Block(
        number=BlockNumber(number),
        hash=BlockHash(_block_hash(number, branch=branch)),
        parent_hash=BlockHash(parent_hash or _block_hash(number - 1, branch=branch)),
        timestamp=datetime(2026, 10, 20, tzinfo=UTC) + timedelta(seconds=number),
    )


def _chain(start: int, end: int, *, branch: int = 0) -> tuple[Block, ...]:
    blocks: list[Block] = []
    parent_hash = _block_hash(start - 1)
    for number in range(start, end + 1):
        block = _block(number, branch=branch, parent_hash=parent_hash)
        blocks.append(block)
        parent_hash = block.hash.value
    return tuple(blocks)


def _replacement_chain(ancestor: Block, end: int, *, branch: int) -> tuple[Block, ...]:
    blocks: list[Block] = []
    parent_hash = ancestor.hash.value
    for number in range(ancestor.number.value + 1, end + 1):
        block = _block(number, branch=branch, parent_hash=parent_hash)
        blocks.append(block)
        parent_hash = block.hash.value
    return tuple(blocks)


def _transaction(number: int, block_number: int, *, branch: int = 0) -> Transaction:
    return Transaction(
        hash=TransactionHash(_transaction_hash(number, branch=branch)),
        block_number=BlockNumber(block_number),
        sender=OTHER,
        to=WALLET,
        value=Wei(number),
        nonce=number,
        gas_limit=21_000,
        input_data=b"",
    )


def _checkpoint(blocks: Sequence[Block]) -> IndexerCheckpoint:
    tip = blocks[-1]
    return IndexerCheckpoint(
        chain_id=ChainId.ethereum_mainnet(),
        start_block_number=BlockNumber(START),
        next_block_number=BlockNumber(tip.number.value + 1),
        expected_parent_hash=tip.hash,
    )


async def _get_postgres_engine(database_url: str) -> AsyncEngine:
    engine: AsyncEngine | None = None
    try:
        engine = create_async_engine(database_url, pool_size=5, max_overflow=0)
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return engine
    except Exception:
        if engine is not None:
            await engine.dispose()
        raise RuntimeError("The dedicated PostgreSQL test database is not reachable") from None


async def _truncate_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("TRUNCATE TABLE indexer_checkpoint, transactions, blocks RESTART IDENTITY CASCADE")
        )


def _run_with_postgres(test_fn: Callable[[AsyncEngine], Awaitable[None]]) -> None:
    settings = _EnvironmentOnlyPostgreSQLTestSettings()
    database_url = prepare_test_database(settings)

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


async def _seed_indexed_chain(
    engine: AsyncEngine,
    blocks: Sequence[Block],
    transactions: Sequence[Transaction],
) -> None:
    session_factory = create_session_factory(engine)
    async with SQLAlchemyIndexerUnitOfWork(session_factory) as unit_of_work:
        for block in blocks:
            await unit_of_work.blocks.save(block)
        for transaction in transactions:
            await unit_of_work.transactions.save(transaction)
        await unit_of_work.checkpoints.create(_checkpoint(blocks))
        await unit_of_work.commit()


async def _append_blocks(
    engine: AsyncEngine,
    blocks: Sequence[Block],
    transactions: Sequence[Transaction] = (),
) -> None:
    session_factory = create_session_factory(engine)
    async with SQLAlchemyIndexerUnitOfWork(session_factory) as unit_of_work:
        checkpoint = await unit_of_work.checkpoints.get_for_update()
        assert checkpoint is not None
        for block in blocks:
            await unit_of_work.blocks.save(block)
            for transaction in transactions:
                if transaction.block_number == block.number:
                    await unit_of_work.transactions.save(transaction)
            checkpoint = await unit_of_work.checkpoints.advance(checkpoint, block)
        await unit_of_work.commit()


async def _rewind_to(engine: AsyncEngine, ancestor_number: int) -> None:
    session_factory = create_session_factory(engine)
    async with SQLAlchemyIndexerUnitOfWork(session_factory) as unit_of_work:
        checkpoint = await unit_of_work.checkpoints.get_for_update()
        ancestor = await unit_of_work.blocks.get_by_number(BlockNumber(ancestor_number))
        assert checkpoint is not None
        assert ancestor is not None
        await unit_of_work.blocks.delete_after(ancestor.number)
        await unit_of_work.checkpoints.rewind(checkpoint, ancestor)
        await unit_of_work.commit()


async def _replace_after(
    engine: AsyncEngine,
    ancestor_number: int,
    replacement_blocks: Sequence[Block],
    replacement_transactions: Sequence[Transaction],
) -> None:
    session_factory = create_session_factory(engine)
    async with SQLAlchemyIndexerUnitOfWork(session_factory) as unit_of_work:
        checkpoint = await unit_of_work.checkpoints.get_for_update()
        ancestor = await unit_of_work.blocks.get_by_number(BlockNumber(ancestor_number))
        assert checkpoint is not None
        assert ancestor is not None
        await unit_of_work.blocks.delete_after(ancestor.number)
        checkpoint = await unit_of_work.checkpoints.rewind(checkpoint, ancestor)
        for block in replacement_blocks:
            await unit_of_work.blocks.save(block)
            for transaction in replacement_transactions:
                if transaction.block_number == block.number:
                    await unit_of_work.transactions.save(transaction)
            checkpoint = await unit_of_work.checkpoints.advance(checkpoint, block)
        await unit_of_work.commit()


async def _snapshot_token(connection: AsyncConnection) -> str:
    token = await connection.scalar(text("SELECT pg_current_snapshot()::text"))
    assert isinstance(token, str)
    return token


async def _bounded_wait(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), timeout=_WAIT_TIMEOUT_SECONDS)


async def _wait_for_barrier_or_task(
    barrier: asyncio.Event,
    task: asyncio.Task[WalletActivityPage] | asyncio.Task[None],
    *,
    barrier_name: str,
) -> None:
    barrier_waiter = asyncio.create_task(barrier.wait())
    try:
        done, _ = await asyncio.wait(
            {barrier_waiter, task},
            timeout=_WAIT_TIMEOUT_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            raise TimeoutError(f"Timed out waiting for {barrier_name}")
        if task.done():
            task.result()
            if not barrier.is_set():
                raise AssertionError(f"Associated task completed without signaling {barrier_name}")
            raise AssertionError(
                f"Associated task completed before the test continued from {barrier_name}"
            )
        assert barrier.is_set()
    finally:
        if not barrier_waiter.done():
            barrier_waiter.cancel()
        await asyncio.gather(barrier_waiter, return_exceptions=True)


class _SnapshotTrackingReader(SQLAlchemyWalletActivityReader):
    snapshot_before: str | None = None
    snapshot_after: str | None = None
    isolation_level: str | None = None

    async def _read_snapshot(
        self,
        connection: AsyncConnection,
        query: WalletActivityQuery,
    ) -> WalletActivityPage:
        page = await super()._read_snapshot(connection, query)
        self.snapshot_after = await _snapshot_token(connection)
        return page


class _PauseAfterCoverageReader(_SnapshotTrackingReader):
    def __init__(
        self,
        engine: AsyncEngine,
        paused: asyncio.Event,
        resume: asyncio.Event,
    ) -> None:
        super().__init__(engine)
        self._paused = paused
        self._resume = resume

    async def _load_coverage(self, connection: AsyncConnection) -> _SnapshotCoverage:
        coverage = await super()._load_coverage(connection)
        isolation_level = await connection.scalar(text("SHOW transaction_isolation"))
        assert isinstance(isolation_level, str)
        self.isolation_level = isolation_level
        self.snapshot_before = await _snapshot_token(connection)
        self._paused.set()
        await _bounded_wait(self._resume)
        return coverage


class _PauseAfterCursorValidationReader(_SnapshotTrackingReader):
    def __init__(
        self,
        engine: AsyncEngine,
        paused: asyncio.Event,
        resume: asyncio.Event,
    ) -> None:
        super().__init__(engine)
        self._paused = paused
        self._resume = resume

    async def _validate_cursor(
        self,
        connection: AsyncConnection,
        query: WalletActivityQuery,
        current: _SnapshotCoverage,
    ) -> _SnapshotCoverage:
        snapshot = await super()._validate_cursor(connection, query, current)
        isolation_level = await connection.scalar(text("SHOW transaction_isolation"))
        assert isinstance(isolation_level, str)
        self.isolation_level = isolation_level
        self.snapshot_before = await _snapshot_token(connection)
        self._paused.set()
        await _bounded_wait(self._resume)
        return snapshot


async def _run_paused_read(
    reader: SQLAlchemyWalletActivityReader,
    query: WalletActivityQuery,
    paused: asyncio.Event,
    resume: asyncio.Event,
    concurrent_action: Callable[[], Awaitable[None]],
) -> WalletActivityPage:
    task = asyncio.create_task(reader.read_page(query))
    try:
        await _wait_for_barrier_or_task(
            paused,
            task,
            barrier_name="reader snapshot pause",
        )
        await asyncio.wait_for(concurrent_action(), timeout=_WAIT_TIMEOUT_SECONDS)
        resume.set()
        return await asyncio.wait_for(task, timeout=_WAIT_TIMEOUT_SECONDS)
    finally:
        resume.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _activity_hashes(page: WalletActivityPage) -> tuple[str, ...]:
    return tuple(activity.transaction_hash.value for activity in page.activities)


def _assert_one_repeatable_read_snapshot(reader: _SnapshotTrackingReader) -> None:
    assert reader.isolation_level == "repeatable read"
    assert reader.snapshot_before is not None
    assert reader.snapshot_after == reader.snapshot_before


def test_snapshot_excludes_append_committed_after_coverage() -> None:
    async def _test(engine: AsyncEngine) -> None:
        blocks = _chain(START, 12)
        old_transaction = _transaction(1, 12)
        await _seed_indexed_chain(engine, blocks, (old_transaction,))
        appended_block = _block(13, parent_hash=blocks[-1].hash.value)
        appended_transaction = _transaction(2, 13)
        paused = asyncio.Event()
        resume = asyncio.Event()
        reader = _PauseAfterCoverageReader(engine, paused, resume)

        page = await _run_paused_read(
            reader,
            WalletActivityQuery(wallet_address=WALLET),
            paused,
            resume,
            lambda: _append_blocks(engine, (appended_block,), (appended_transaction,)),
        )
        fresh = await SQLAlchemyWalletActivityReader(engine).read_page(
            WalletActivityQuery(wallet_address=WALLET)
        )

        _assert_one_repeatable_read_snapshot(reader)
        assert page.coverage.indexed_through_block == BlockNumber(12)
        assert page.coverage.indexed_through_hash == blocks[-1].hash
        assert _activity_hashes(page) == (old_transaction.hash.value,)
        assert fresh.coverage.indexed_through_block == BlockNumber(13)
        assert fresh.coverage.indexed_through_hash == appended_block.hash
        assert _activity_hashes(fresh) == (
            appended_transaction.hash.value,
            old_transaction.hash.value,
        )

    _run_with_postgres(_test)


def test_snapshot_preserves_pre_rewind_state() -> None:
    async def _test(engine: AsyncEngine) -> None:
        blocks = _chain(START, 12)
        retained_transaction = _transaction(1, 11)
        deleted_transactions = (_transaction(2, 12), _transaction(3, 12))
        await _seed_indexed_chain(
            engine,
            blocks,
            (retained_transaction, *deleted_transactions),
        )
        first_page = await SQLAlchemyWalletActivityReader(engine).read_page(
            WalletActivityQuery(wallet_address=WALLET, limit=1)
        )
        cursor = first_page.next_cursor
        assert cursor is not None
        paused = asyncio.Event()
        resume = asyncio.Event()
        reader = _PauseAfterCoverageReader(engine, paused, resume)

        page = await _run_paused_read(
            reader,
            WalletActivityQuery(wallet_address=WALLET),
            paused,
            resume,
            lambda: _rewind_to(engine, 11),
        )
        fresh_reader = SQLAlchemyWalletActivityReader(engine)
        fresh = await fresh_reader.read_page(WalletActivityQuery(wallet_address=WALLET))

        _assert_one_repeatable_read_snapshot(reader)
        assert page.coverage.indexed_through_block == BlockNumber(12)
        assert page.coverage.indexed_through_hash == blocks[-1].hash
        assert _activity_hashes(page) == (
            deleted_transactions[1].hash.value,
            deleted_transactions[0].hash.value,
            retained_transaction.hash.value,
        )
        assert fresh.coverage.indexed_through_block == BlockNumber(11)
        assert fresh.coverage.indexed_through_hash == blocks[1].hash
        assert _activity_hashes(fresh) == (retained_transaction.hash.value,)
        with pytest.raises(StaleWalletActivityCursorError, match="no longer indexed"):
            await fresh_reader.read_page(
                WalletActivityQuery(wallet_address=WALLET, limit=1, cursor=cursor)
            )

    _run_with_postgres(_test)


def test_cursor_validation_and_page_share_snapshot_during_replacement() -> None:
    async def _test(engine: AsyncEngine) -> None:
        blocks = _chain(START, 12)
        transactions = (
            _transaction(1, 11),
            _transaction(2, 12),
            _transaction(3, 12),
        )
        await _seed_indexed_chain(engine, blocks, transactions)
        first_page = await SQLAlchemyWalletActivityReader(engine).read_page(
            WalletActivityQuery(wallet_address=WALLET, limit=1)
        )
        cursor = first_page.next_cursor
        assert cursor is not None
        replacement = _replacement_chain(blocks[1], 12, branch=1)
        replacement_transactions = (
            _transaction(12, 12, branch=1),
            _transaction(13, 12, branch=1),
        )
        paused = asyncio.Event()
        resume = asyncio.Event()
        reader = _PauseAfterCursorValidationReader(engine, paused, resume)

        continued = await _run_paused_read(
            reader,
            WalletActivityQuery(wallet_address=WALLET, limit=1, cursor=cursor),
            paused,
            resume,
            lambda: _replace_after(
                engine,
                11,
                replacement,
                replacement_transactions,
            ),
        )

        _assert_one_repeatable_read_snapshot(reader)
        assert first_page.activities[0].transaction_hash == transactions[2].hash
        assert continued.activities[0].transaction_hash == transactions[1].hash
        assert (
            len(
                {
                    first_page.activities[0].transaction_hash,
                    continued.activities[0].transaction_hash,
                }
            )
            == 2
        )
        assert continued.coverage.indexed_through_hash == blocks[-1].hash

        fresh_reader = SQLAlchemyWalletActivityReader(engine)
        with pytest.raises(StaleWalletActivityCursorError, match="snapshot was replaced"):
            await fresh_reader.read_page(
                WalletActivityQuery(wallet_address=WALLET, limit=1, cursor=cursor)
            )
        fresh = await fresh_reader.read_page(WalletActivityQuery(wallet_address=WALLET))
        assert fresh.coverage.indexed_through_hash == replacement[-1].hash
        assert _activity_hashes(fresh) == (
            replacement_transactions[1].hash.value,
            replacement_transactions[0].hash.value,
            transactions[0].hash.value,
        )

    _run_with_postgres(_test)


def test_reader_before_and_after_reorg_commit_sees_only_complete_states() -> None:
    async def _test(engine: AsyncEngine) -> None:
        blocks = _chain(START, 12)
        retained_transaction = _transaction(1, 11)
        deleted_transaction = _transaction(2, 12)
        await _seed_indexed_chain(
            engine,
            blocks,
            (retained_transaction, deleted_transaction),
        )
        mutation_staged = asyncio.Event()
        allow_commit = asyncio.Event()

        async def _staged_rewind() -> None:
            session_factory = create_session_factory(engine)
            async with SQLAlchemyIndexerUnitOfWork(session_factory) as unit_of_work:
                checkpoint = await unit_of_work.checkpoints.get_for_update()
                ancestor = await unit_of_work.blocks.get_by_number(BlockNumber(11))
                assert checkpoint is not None
                assert ancestor is not None
                await unit_of_work.blocks.delete_after(ancestor.number)
                await unit_of_work.checkpoints.rewind(checkpoint, ancestor)
                mutation_staged.set()
                await _bounded_wait(allow_commit)
                await unit_of_work.commit()

        writer = asyncio.create_task(_staged_rewind())
        try:
            await _wait_for_barrier_or_task(
                mutation_staged,
                writer,
                barrier_name="staged reorganization mutation",
            )
            before_commit = await asyncio.wait_for(
                SQLAlchemyWalletActivityReader(engine).read_page(
                    WalletActivityQuery(wallet_address=WALLET)
                ),
                timeout=_WAIT_TIMEOUT_SECONDS,
            )
            allow_commit.set()
            await asyncio.wait_for(writer, timeout=_WAIT_TIMEOUT_SECONDS)
        finally:
            allow_commit.set()
            if not writer.done():
                writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)

        after_commit = await SQLAlchemyWalletActivityReader(engine).read_page(
            WalletActivityQuery(wallet_address=WALLET)
        )
        assert before_commit.coverage.indexed_through_block == BlockNumber(12)
        assert before_commit.coverage.indexed_through_hash == blocks[-1].hash
        assert _activity_hashes(before_commit) == (
            deleted_transaction.hash.value,
            retained_transaction.hash.value,
        )
        assert after_commit.coverage.indexed_through_block == BlockNumber(11)
        assert after_commit.coverage.indexed_through_hash == blocks[1].hash
        assert _activity_hashes(after_commit) == (retained_transaction.hash.value,)

    _run_with_postgres(_test)


@pytest.mark.parametrize("scenario", ["reorg_above_anchor", "boundary_moved"])
def test_cursor_reorganization_matrix_additional_races(scenario: str) -> None:
    async def _test(engine: AsyncEngine) -> None:
        blocks = _chain(START, 12)
        transactions = (
            _transaction(1, 11),
            _transaction(2, 12),
            _transaction(3, 12),
        )
        await _seed_indexed_chain(engine, blocks, transactions)
        reader = SQLAlchemyWalletActivityReader(engine)
        first_page = await reader.read_page(WalletActivityQuery(wallet_address=WALLET, limit=1))
        cursor = first_page.next_cursor
        assert cursor is not None

        if scenario == "reorg_above_anchor":
            appended = _replacement_chain(blocks[-1], 14, branch=1)
            await _append_blocks(engine, appended)
            replacement = _replacement_chain(blocks[-1], 14, branch=2)
            await _replace_after(engine, 12, replacement, ())

            continued = await reader.read_page(
                WalletActivityQuery(wallet_address=WALLET, limit=1, cursor=cursor)
            )
            assert continued.coverage.indexed_through_block == BlockNumber(12)
            assert continued.coverage.indexed_through_hash == blocks[-1].hash
            assert continued.activities[0].transaction_hash == transactions[1].hash
            current = await reader.read_page(WalletActivityQuery(wallet_address=WALLET))
            assert current.coverage.indexed_through_block == BlockNumber(14)
            assert current.coverage.indexed_through_hash == replacement[-1].hash
        else:
            async with engine.begin() as connection:
                await connection.execute(
                    update(TransactionModel)
                    .where(TransactionModel.hash == cursor.last_transaction_hash.value)
                    .values(block_number=11)
                )
            with pytest.raises(StaleWalletActivityCursorError, match="no longer matches"):
                await reader.read_page(
                    WalletActivityQuery(wallet_address=WALLET, limit=1, cursor=cursor)
                )
            async with engine.connect() as connection:
                moved_to = await connection.scalar(
                    select(TransactionModel.block_number).where(
                        TransactionModel.hash == cursor.last_transaction_hash.value
                    )
                )
            assert moved_to == 11

    _run_with_postgres(_test)
