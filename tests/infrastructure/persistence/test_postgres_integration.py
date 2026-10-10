"""PostgreSQL asyncpg integration tests.

These tests run against a real PostgreSQL instance when configured and reachable.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ethereum_wallet_intelligence.application import PersistenceConflictError
from ethereum_wallet_intelligence.domain.ethereum import (
    Block,
    BlockHash,
    BlockNumber,
    EthereumAddress,
    Transaction,
    TransactionHash,
    Wei,
)
from ethereum_wallet_intelligence.infrastructure.persistence.database import create_session_factory
from ethereum_wallet_intelligence.infrastructure.persistence.mappers import transaction_to_model
from ethereum_wallet_intelligence.infrastructure.persistence.repositories import (
    SQLAlchemyBlockRepository,
    SQLAlchemyTransactionRepository,
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
        await conn.execute(text("TRUNCATE TABLE transactions, blocks RESTART IDENTITY CASCADE;"))


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
