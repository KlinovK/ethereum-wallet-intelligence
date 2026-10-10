"""Unit tests for SQLAlchemy repository adapters."""

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ethereum_wallet_intelligence.application import (
    BlockRepository,
    PersistenceConflictError,
    TransactionRepository,
)
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
from ethereum_wallet_intelligence.infrastructure.persistence.models import Base
from ethereum_wallet_intelligence.infrastructure.persistence.repositories import (
    SQLAlchemyBlockRepository,
    SQLAlchemyTransactionRepository,
)

BLOCK_HASH_1 = "0x" + "11" * 32
BLOCK_HASH_2 = "0x" + "22" * 32
PARENT_HASH_1 = "0x" + "33" * 32
PARENT_HASH_2 = "0x" + "44" * 32
TX_HASH_1 = "0x" + "55" * 32
TX_HASH_2 = "0x" + "66" * 32
SENDER_1 = "0x" + "aa" * 20
SENDER_2 = "0x" + "bb" * 20
RECIPIENT_1 = "0x" + "cc" * 20
RECIPIENT_2 = "0x" + "dd" * 20
UINT256_MAX = 2**256 - 1


async def _init_test_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _make_sample_block(
    number: int = 20_000_000,
    hash_str: str = BLOCK_HASH_1,
    parent_hash_str: str = PARENT_HASH_1,
    timestamp: datetime | None = None,
) -> Block:
    return Block(
        number=BlockNumber(number),
        hash=BlockHash(hash_str),
        parent_hash=BlockHash(parent_hash_str),
        timestamp=timestamp or datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
    )


def _make_sample_tx(
    hash_str: str = TX_HASH_1,
    block_number: int = 20_000_000,
    sender_str: str = SENDER_1,
    to_str: str | None = RECIPIENT_1,
    value: int = 10**18,
    nonce: int = 0,
    gas_limit: int = 21_000,
    input_data: bytes = b"\x01\x02\x03",
) -> Transaction:
    return Transaction(
        hash=TransactionHash(hash_str),
        block_number=BlockNumber(block_number),
        sender=EthereumAddress(sender_str),
        to=EthereumAddress(to_str) if to_str is not None else None,
        value=Wei(value),
        nonce=nonce,
        gas_limit=gas_limit,
        input_data=input_data,
    )


def test_repositories_satisfy_application_protocols() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = create_session_factory(engine)

    async def _check() -> None:
        async with session_factory() as session:
            block_repo: BlockRepository = SQLAlchemyBlockRepository(session)
            tx_repo: TransactionRepository = SQLAlchemyTransactionRepository(session)
            assert isinstance(block_repo, SQLAlchemyBlockRepository)
            assert isinstance(tx_repo, SQLAlchemyTransactionRepository)

    asyncio.run(_check())


def test_block_repository_get_nonexistent_returns_none() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    asyncio.run(_init_test_db(engine))
    session_factory = create_session_factory(engine)

    async def _run() -> None:
        async with session_factory() as session:
            repo = SQLAlchemyBlockRepository(session)
            result = await repo.get_by_number(BlockNumber(12345))
            assert result is None

    asyncio.run(_run())


def test_block_repository_save_and_get() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    asyncio.run(_init_test_db(engine))
    session_factory = create_session_factory(engine)
    block = _make_sample_block()

    async def _run() -> None:
        async with session_factory() as session, session.begin():
            repo = SQLAlchemyBlockRepository(session)
            await repo.save(block)

        async with session_factory() as session:
            repo = SQLAlchemyBlockRepository(session)
            fetched = await repo.get_by_number(block.number)
            assert fetched == block

    asyncio.run(_run())


def test_block_repository_idempotent_save() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    asyncio.run(_init_test_db(engine))
    session_factory = create_session_factory(engine)
    block = _make_sample_block()

    async def _run() -> None:
        async with session_factory() as session, session.begin():
            repo = SQLAlchemyBlockRepository(session)
            await repo.save(block)
            await repo.save(block)  # Duplicate save in same session

        async with session_factory() as session, session.begin():
            repo = SQLAlchemyBlockRepository(session)
            await repo.save(block)  # Duplicate save across sessions

        async with session_factory() as session:
            repo = SQLAlchemyBlockRepository(session)
            fetched = await repo.get_by_number(block.number)
            assert fetched == block

    asyncio.run(_run())


@pytest.mark.parametrize(
    ("conflicting_block", "match_str"),
    [
        (
            _make_sample_block(number=20_000_000, hash_str=BLOCK_HASH_2),
            "Conflicting block already exists",
        ),
        (
            _make_sample_block(number=20_000_000, parent_hash_str=PARENT_HASH_2),
            "Conflicting block already exists",
        ),
        (
            _make_sample_block(
                number=20_000_000,
                timestamp=datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC),
            ),
            "Conflicting block already exists",
        ),
        (
            _make_sample_block(number=20_000_001, hash_str=BLOCK_HASH_1),
            "Conflicting block already exists",
        ),
    ],
)
def test_block_repository_rejects_conflicting_block(
    conflicting_block: Block, match_str: str
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    asyncio.run(_init_test_db(engine))
    session_factory = create_session_factory(engine)
    original_block = _make_sample_block(number=20_000_000, hash_str=BLOCK_HASH_1)

    async def _run() -> None:
        async with session_factory() as session, session.begin():
            repo = SQLAlchemyBlockRepository(session)
            await repo.save(original_block)

        async with session_factory() as session, session.begin():
            repo = SQLAlchemyBlockRepository(session)
            with pytest.raises(PersistenceConflictError, match=match_str):
                await repo.save(conflicting_block)

    asyncio.run(_run())


def test_block_repository_transaction_rollback_boundary() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    asyncio.run(_init_test_db(engine))
    session_factory = create_session_factory(engine)
    block = _make_sample_block()

    async def _run() -> None:
        # Session saves without commit, then rolls back
        async with session_factory() as session:
            repo = SQLAlchemyBlockRepository(session)
            await repo.save(block)
            await session.rollback()

        async with session_factory() as session:
            repo = SQLAlchemyBlockRepository(session)
            fetched = await repo.get_by_number(block.number)
            assert fetched is None

    asyncio.run(_run())


def test_transaction_repository_get_nonexistent_returns_none() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    asyncio.run(_init_test_db(engine))
    session_factory = create_session_factory(engine)

    async def _run() -> None:
        async with session_factory() as session:
            repo = SQLAlchemyTransactionRepository(session)
            result = await repo.get_by_hash(TransactionHash(TX_HASH_1))
            assert result is None

    asyncio.run(_run())


def test_transaction_repository_save_and_get_roundtrip() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    asyncio.run(_init_test_db(engine))
    session_factory = create_session_factory(engine)
    block = _make_sample_block()
    tx = _make_sample_tx(value=UINT256_MAX, input_data=b"\xaa\xbb\xcc\xdd")

    async def _run() -> None:
        async with session_factory() as session, session.begin():
            block_repo = SQLAlchemyBlockRepository(session)
            await block_repo.save(block)
            tx_repo = SQLAlchemyTransactionRepository(session)
            await tx_repo.save(tx)

        async with session_factory() as session:
            tx_repo = SQLAlchemyTransactionRepository(session)
            fetched = await tx_repo.get_by_hash(tx.hash)
            assert fetched == tx
            assert fetched is not None
            assert fetched.value == Wei(UINT256_MAX)
            assert fetched.input_data == b"\xaa\xbb\xcc\xdd"

    asyncio.run(_run())


def test_transaction_repository_save_contract_creation_null_recipient() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    asyncio.run(_init_test_db(engine))
    session_factory = create_session_factory(engine)
    block = _make_sample_block()
    tx = _make_sample_tx(to_str=None, value=0, input_data=b"\x60\x80")

    async def _run() -> None:
        async with session_factory() as session, session.begin():
            block_repo = SQLAlchemyBlockRepository(session)
            await block_repo.save(block)
            tx_repo = SQLAlchemyTransactionRepository(session)
            await tx_repo.save(tx)

        async with session_factory() as session:
            tx_repo = SQLAlchemyTransactionRepository(session)
            fetched = await tx_repo.get_by_hash(tx.hash)
            assert fetched == tx
            assert fetched is not None
            assert fetched.to is None

    asyncio.run(_run())


def test_transaction_repository_idempotent_save() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    asyncio.run(_init_test_db(engine))
    session_factory = create_session_factory(engine)
    block = _make_sample_block()
    tx = _make_sample_tx()

    async def _run() -> None:
        async with session_factory() as session, session.begin():
            block_repo = SQLAlchemyBlockRepository(session)
            await block_repo.save(block)
            tx_repo = SQLAlchemyTransactionRepository(session)
            await tx_repo.save(tx)
            await tx_repo.save(tx)

        async with session_factory() as session, session.begin():
            tx_repo = SQLAlchemyTransactionRepository(session)
            await tx_repo.save(tx)

        async with session_factory() as session:
            tx_repo = SQLAlchemyTransactionRepository(session)
            fetched = await tx_repo.get_by_hash(tx.hash)
            assert fetched == tx

    asyncio.run(_run())


@pytest.mark.parametrize(
    "conflicting_tx",
    [
        _make_sample_tx(hash_str=TX_HASH_1, block_number=20_000_001),
        _make_sample_tx(hash_str=TX_HASH_1, sender_str=SENDER_2),
        _make_sample_tx(hash_str=TX_HASH_1, to_str=RECIPIENT_2),
        _make_sample_tx(hash_str=TX_HASH_1, to_str=None),
        _make_sample_tx(hash_str=TX_HASH_1, value=999),
        _make_sample_tx(hash_str=TX_HASH_1, nonce=99),
        _make_sample_tx(hash_str=TX_HASH_1, gas_limit=50_000),
        _make_sample_tx(hash_str=TX_HASH_1, input_data=b"\xff\xff"),
    ],
)
def test_transaction_repository_rejects_conflicting_transaction(
    conflicting_tx: Transaction,
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    asyncio.run(_init_test_db(engine))
    session_factory = create_session_factory(engine)
    block = _make_sample_block(number=20_000_000)
    block2 = _make_sample_block(number=20_000_001, hash_str=BLOCK_HASH_2)
    original_tx = _make_sample_tx(hash_str=TX_HASH_1, block_number=20_000_000)

    async def _run() -> None:
        async with session_factory() as session, session.begin():
            block_repo = SQLAlchemyBlockRepository(session)
            await block_repo.save(block)
            await block_repo.save(block2)
            tx_repo = SQLAlchemyTransactionRepository(session)
            await tx_repo.save(original_tx)

        async with session_factory() as session, session.begin():
            tx_repo = SQLAlchemyTransactionRepository(session)
            with pytest.raises(
                PersistenceConflictError, match="Conflicting transaction already exists"
            ):
                await tx_repo.save(conflicting_tx)

    asyncio.run(_run())


def test_transaction_repository_rejects_missing_foreign_block() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    asyncio.run(_init_test_db(engine))
    session_factory = create_session_factory(engine)
    tx = _make_sample_tx(block_number=99_999_999)

    async def _run() -> None:
        async with session_factory() as session, session.begin():
            tx_repo = SQLAlchemyTransactionRepository(session)
            with pytest.raises(
                PersistenceConflictError, match="Referenced block 99999999 does not exist"
            ):
                await tx_repo.save(tx)

    asyncio.run(_run())


def test_transaction_repository_transaction_rollback_boundary() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    asyncio.run(_init_test_db(engine))
    session_factory = create_session_factory(engine)
    block = _make_sample_block()
    tx = _make_sample_tx()

    async def _run() -> None:
        async with session_factory() as session, session.begin():
            block_repo = SQLAlchemyBlockRepository(session)
            await block_repo.save(block)

        async with session_factory() as session:
            tx_repo = SQLAlchemyTransactionRepository(session)
            await tx_repo.save(tx)
            await session.rollback()

        async with session_factory() as session:
            tx_repo = SQLAlchemyTransactionRepository(session)
            fetched = await tx_repo.get_by_hash(tx.hash)
            assert fetched is None

    asyncio.run(_run())
