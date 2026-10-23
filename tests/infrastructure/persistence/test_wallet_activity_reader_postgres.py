"""Isolated PostgreSQL tests for the wallet activity read adapter."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from ethereum_wallet_intelligence.application import (
    StaleWalletActivityCursorError,
    WalletActivityDirection,
    WalletActivityIndexUnavailableError,
    WalletActivityPage,
    WalletActivityQuery,
    WalletActivityReadError,
)
from ethereum_wallet_intelligence.domain.ethereum import EthereumAddress
from ethereum_wallet_intelligence.infrastructure.persistence.models import (
    BlockModel,
    IndexerCheckpointModel,
    TransactionModel,
)
from ethereum_wallet_intelligence.infrastructure.persistence.wallet_activity_reader import (
    SQLAlchemyWalletActivityReader,
)
from tests.support.postgres import prepare_test_database

pytestmark = pytest.mark.integration

WALLET = EthereumAddress("0x" + "11" * 20)
OTHER = EthereumAddress("0x" + "22" * 20)
UNKNOWN = EthereumAddress("0x" + "33" * 20)
START = 10


def _block_hash(number: int, *, branch: int = 0) -> str:
    return f"0x{branch:02x}{number:062x}"


def _tx_hash(number: int) -> str:
    return f"0x{number:064x}"


async def _get_postgres_engine(database_url: str) -> AsyncEngine:
    engine: AsyncEngine | None = None
    try:
        engine = create_async_engine(database_url)
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


async def _seed(
    engine: AsyncEngine,
    *,
    block_numbers: Sequence[int] = (10, 11, 12),
    transactions: Sequence[dict[str, object]] = (),
    checkpoint: bool = True,
    chain_id: int = 1,
    next_block: int | None = None,
    expected_parent_hash: str | None = None,
) -> None:
    async with engine.begin() as connection:
        if block_numbers:
            await connection.execute(
                insert(BlockModel),
                [
                    {
                        "number": number,
                        "hash": _block_hash(number),
                        "parent_hash": _block_hash(number - 1),
                        "timestamp": datetime(2026, 10, 20, tzinfo=UTC) + timedelta(seconds=number),
                    }
                    for number in block_numbers
                ],
            )
        if transactions:
            await connection.execute(insert(TransactionModel), list(transactions))
        if checkpoint:
            resolved_next = next_block if next_block is not None else max(block_numbers) + 1
            parent_hash = expected_parent_hash or (
                _block_hash(resolved_next - 1) if resolved_next > START else _block_hash(START - 1)
            )
            await connection.execute(
                insert(IndexerCheckpointModel).values(
                    id=1,
                    chain_id=chain_id,
                    start_block_number=START,
                    next_block_number=resolved_next,
                    expected_parent_hash=parent_hash,
                )
            )


def _transaction(
    number: int,
    *,
    block_number: int = 12,
    sender: str = OTHER.value,
    recipient: str | None = WALLET.value,
    value: int = 1,
    input_data: bytes = b"",
) -> dict[str, object]:
    return {
        "hash": _tx_hash(number),
        "block_number": block_number,
        "sender": sender,
        "recipient": recipient,
        "value_wei": Decimal(value),
        "nonce": number,
        "gas_limit": 21_000,
        "input": input_data,
    }


def test_postgres_reads_all_disjoint_directions_and_special_transactions() -> None:
    async def _test(engine: AsyncEngine) -> None:
        await _seed(
            engine,
            transactions=(
                _transaction(1),
                _transaction(
                    2,
                    sender=WALLET.value,
                    recipient=None,
                    value=0,
                    input_data=b"\x60\x80",
                ),
                _transaction(3, sender=WALLET.value, recipient=WALLET.value, value=0),
                _transaction(4, sender=OTHER.value, recipient=UNKNOWN.value),
            ),
        )
        reader = SQLAlchemyWalletActivityReader(engine)

        all_page = await reader.read_page(WalletActivityQuery(wallet_address=WALLET))
        incoming = await reader.read_page(
            WalletActivityQuery(
                wallet_address=WALLET,
                direction=WalletActivityDirection.INCOMING,
            )
        )
        outgoing = await reader.read_page(
            WalletActivityQuery(
                wallet_address=WALLET,
                direction=WalletActivityDirection.OUTGOING,
            )
        )
        self_page = await reader.read_page(
            WalletActivityQuery(
                wallet_address=WALLET,
                direction=WalletActivityDirection.SELF,
            )
        )

        assert [item.transaction_hash.value for item in all_page.activities] == [
            _tx_hash(3),
            _tx_hash(2),
            _tx_hash(1),
        ]
        assert [item.direction for item in all_page.activities] == [
            WalletActivityDirection.SELF,
            WalletActivityDirection.OUTGOING,
            WalletActivityDirection.INCOMING,
        ]
        assert incoming.activities[0].transaction_hash.value == _tx_hash(1)
        assert outgoing.activities[0].is_contract_creation is True
        assert outgoing.activities[0].value.value == 0
        assert outgoing.activities[0].has_input is True
        assert self_page.activities[0].transaction_hash.value == _tx_hash(3)

    _run_with_postgres(_test)


def test_postgres_keyset_paginates_across_and_within_blocks() -> None:
    async def _test(engine: AsyncEngine) -> None:
        await _seed(
            engine,
            transactions=(
                _transaction(1, block_number=10),
                _transaction(2, block_number=11),
                _transaction(3, block_number=12),
                _transaction(4, block_number=12),
                _transaction(5, block_number=12),
            ),
        )
        reader = SQLAlchemyWalletActivityReader(engine)
        pages: list[WalletActivityPage] = []
        cursor = None
        while True:
            page = await reader.read_page(
                WalletActivityQuery(wallet_address=WALLET, limit=2, cursor=cursor)
            )
            pages.append(page)
            cursor = page.next_cursor
            if cursor is None:
                break

        hashes = [activity.transaction_hash.value for page in pages for activity in page.activities]
        assert hashes == [
            _tx_hash(5),
            _tx_hash(4),
            _tx_hash(3),
            _tx_hash(2),
            _tx_hash(1),
        ]
        assert len(hashes) == len(set(hashes))
        assert [len(page.activities) for page in pages] == [2, 2, 1]
        assert all(page.coverage.indexed_through_block is not None for page in pages)
        assert all(
            page.coverage.indexed_through_block is not None
            and page.coverage.indexed_through_block.value == 12
            for page in pages
        )

    _run_with_postgres(_test)


def test_postgres_unknown_wallet_returns_empty_page() -> None:
    async def _test(engine: AsyncEngine) -> None:
        await _seed(engine, transactions=(_transaction(1),))

        page = await SQLAlchemyWalletActivityReader(engine).read_page(
            WalletActivityQuery(wallet_address=UNKNOWN)
        )

        assert page.activities == ()
        assert page.next_cursor is None
        assert page.coverage.indexed_through_block is not None
        assert page.coverage.indexed_through_block.value == 12

    _run_with_postgres(_test)


def test_postgres_unstarted_and_missing_checkpoint_are_distinguished() -> None:
    async def _test(engine: AsyncEngine) -> None:
        await _seed(engine, block_numbers=(), next_block=START)
        reader = SQLAlchemyWalletActivityReader(engine)

        page = await reader.read_page(WalletActivityQuery(wallet_address=WALLET))
        assert page.activities == ()
        assert page.coverage.indexed_through_block is None

        async with engine.begin() as connection:
            await connection.execute(delete(IndexerCheckpointModel))
        with pytest.raises(WalletActivityIndexUnavailableError, match="not been initialized"):
            await reader.read_page(WalletActivityQuery(wallet_address=WALLET))

        async with engine.begin() as connection:
            await connection.execute(
                insert(BlockModel).values(
                    number=START,
                    hash=_block_hash(START),
                    parent_hash=_block_hash(START - 1),
                    timestamp=datetime(2026, 10, 20, tzinfo=UTC),
                )
            )
        with pytest.raises(WalletActivityReadError, match="without an indexing checkpoint"):
            await reader.read_page(WalletActivityQuery(wallet_address=WALLET))

        async with engine.begin() as connection:
            await connection.execute(
                insert(IndexerCheckpointModel).values(
                    id=1,
                    chain_id=1,
                    start_block_number=START,
                    next_block_number=START,
                    expected_parent_hash=_block_hash(START - 1),
                )
            )
        with pytest.raises(WalletActivityReadError, match="Unstarted wallet activity index"):
            await reader.read_page(WalletActivityQuery(wallet_address=WALLET))

    _run_with_postgres(_test)


def test_postgres_cursor_allows_advancement_and_rejects_changed_anchor() -> None:
    async def _test(engine: AsyncEngine) -> None:
        await _seed(
            engine,
            transactions=(
                _transaction(1, block_number=11),
                _transaction(2, block_number=12),
            ),
        )
        reader = SQLAlchemyWalletActivityReader(engine)
        first = await reader.read_page(WalletActivityQuery(wallet_address=WALLET, limit=1))
        cursor = first.next_cursor
        assert cursor is not None

        async with engine.begin() as connection:
            await connection.execute(
                insert(BlockModel).values(
                    number=13,
                    hash=_block_hash(13),
                    parent_hash=_block_hash(12),
                    timestamp=datetime(2026, 10, 20, tzinfo=UTC),
                )
            )
            await connection.execute(
                update(IndexerCheckpointModel)
                .where(IndexerCheckpointModel.id == 1)
                .values(next_block_number=14, expected_parent_hash=_block_hash(13))
            )

        advanced = await reader.read_page(
            WalletActivityQuery(wallet_address=WALLET, limit=1, cursor=cursor)
        )
        assert advanced.coverage.indexed_through_block is not None
        assert advanced.coverage.indexed_through_block.value == 12
        assert advanced.activities[0].transaction_hash.value == _tx_hash(1)

        async with engine.begin() as connection:
            await connection.execute(
                update(BlockModel)
                .where(BlockModel.number == cursor.snapshot_block_number.value)
                .values(hash=_block_hash(12, branch=1))
            )
        with pytest.raises(StaleWalletActivityCursorError, match="snapshot was replaced"):
            await reader.read_page(
                WalletActivityQuery(wallet_address=WALLET, limit=1, cursor=cursor)
            )

    _run_with_postgres(_test)


def test_postgres_cursor_rejects_rewind_and_missing_boundary() -> None:
    async def _test(engine: AsyncEngine) -> None:
        await _seed(
            engine,
            transactions=(
                _transaction(1, block_number=11),
                _transaction(2, block_number=12),
            ),
        )
        reader = SQLAlchemyWalletActivityReader(engine)
        first = await reader.read_page(WalletActivityQuery(wallet_address=WALLET, limit=1))
        cursor = first.next_cursor
        assert cursor is not None

        async with engine.begin() as connection:
            await connection.execute(
                delete(TransactionModel).where(
                    TransactionModel.hash == cursor.last_transaction_hash.value
                )
            )
        with pytest.raises(StaleWalletActivityCursorError, match="boundary transaction"):
            await reader.read_page(
                WalletActivityQuery(wallet_address=WALLET, limit=1, cursor=cursor)
            )

        async with engine.begin() as connection:
            await connection.execute(delete(BlockModel).where(BlockModel.number == 12))
            await connection.execute(
                update(IndexerCheckpointModel)
                .where(IndexerCheckpointModel.id == 1)
                .values(next_block_number=12, expected_parent_hash=_block_hash(11))
            )
        with pytest.raises(StaleWalletActivityCursorError, match="no longer indexed"):
            await reader.read_page(
                WalletActivityQuery(wallet_address=WALLET, limit=1, cursor=cursor)
            )

        async with engine.begin() as connection:
            await connection.execute(delete(BlockModel))
            await connection.execute(
                update(IndexerCheckpointModel)
                .where(IndexerCheckpointModel.id == 1)
                .values(
                    next_block_number=START,
                    expected_parent_hash=_block_hash(START - 1),
                )
            )
        with pytest.raises(StaleWalletActivityCursorError, match="no longer indexed"):
            await reader.read_page(
                WalletActivityQuery(wallet_address=WALLET, limit=1, cursor=cursor)
            )

    _run_with_postgres(_test)


def test_postgres_rejects_corrupted_checkpoint_coverage() -> None:
    async def _test(engine: AsyncEngine) -> None:
        await _seed(engine, chain_id=8453)
        reader = SQLAlchemyWalletActivityReader(engine)
        with pytest.raises(WalletActivityReadError, match="not Ethereum Mainnet"):
            await reader.read_page(WalletActivityQuery(wallet_address=WALLET))

        async with engine.begin() as connection:
            await connection.execute(
                update(IndexerCheckpointModel)
                .where(IndexerCheckpointModel.id == 1)
                .values(chain_id=1, expected_parent_hash=_block_hash(12, branch=1))
            )
        with pytest.raises(WalletActivityReadError, match="tip hash"):
            await reader.read_page(WalletActivityQuery(wallet_address=WALLET))

    _run_with_postgres(_test)


def test_postgres_configures_repeatable_read_read_only_and_local_timeout() -> None:
    class _InspectingReader(SQLAlchemyWalletActivityReader):
        settings: tuple[str, str, str] | None = None

        async def _read_snapshot(
            self,
            connection: AsyncConnection,
            query: WalletActivityQuery,
        ) -> WalletActivityPage:
            self.settings = (
                str(await connection.scalar(text("SHOW transaction_isolation"))),
                str(await connection.scalar(text("SHOW transaction_read_only"))),
                str(await connection.scalar(text("SHOW statement_timeout"))),
            )
            return await super()._read_snapshot(connection, query)

    async def _test(engine: AsyncEngine) -> None:
        await _seed(engine)
        reader = _InspectingReader(engine, statement_timeout_ms=1_234)
        await reader.read_page(WalletActivityQuery(wallet_address=WALLET))

        assert reader.settings == ("repeatable read", "on", "1234ms")

    _run_with_postgres(_test)


def test_postgres_read_only_transaction_rejects_writes() -> None:
    class _WritingReader(SQLAlchemyWalletActivityReader):
        write_succeeded = False

        async def _read_snapshot(
            self,
            connection: AsyncConnection,
            query: WalletActivityQuery,
        ) -> WalletActivityPage:
            await connection.execute(
                update(IndexerCheckpointModel)
                .where(IndexerCheckpointModel.id == 1)
                .values(chain_id=2)
            )
            self.write_succeeded = True
            return await super()._read_snapshot(connection, query)

    async def _test(engine: AsyncEngine) -> None:
        await _seed(engine)

        reader = _WritingReader(engine)
        with pytest.raises(
            WalletActivityIndexUnavailableError,
            match="database read failed safely",
        ):
            await reader.read_page(WalletActivityQuery(wallet_address=WALLET))

        assert reader.write_succeeded is False
        async with engine.connect() as connection:
            chain_id = await connection.scalar(select(IndexerCheckpointModel.chain_id))
        assert chain_id == 1

    _run_with_postgres(_test)


def test_postgres_wallet_activity_index_migration_upgrade_and_downgrade() -> None:
    database_url = prepare_test_database()
    config = Config("alembic.ini")
    config.attributes["database_url_override"] = database_url
    expected = {
        "ix_transactions_sender_activity_keyset",
        "ix_transactions_recipient_activity_keyset",
    }
    existing = {
        "ix_transactions_sender",
        "ix_transactions_recipient",
        "ix_transactions_block_number",
    }

    async def _indexes() -> dict[str, str]:
        engine = await _get_postgres_engine(database_url)
        try:
            async with engine.connect() as connection:
                rows = (
                    await connection.execute(
                        text(
                            "SELECT indexname, indexdef FROM pg_indexes "
                            "WHERE schemaname = current_schema() "
                            "AND tablename = 'transactions'"
                        )
                    )
                ).all()
            return {str(name): str(definition) for name, definition in rows}
        finally:
            await engine.dispose()

    try:
        before = asyncio.run(_indexes())
        assert expected <= before.keys()
        assert existing <= before.keys()
        assert (
            '(sender, block_number DESC, hash COLLATE "C" DESC)'
            in before["ix_transactions_sender_activity_keyset"]
        )
        assert (
            '(recipient, block_number DESC, hash COLLATE "C" DESC)'
            in before["ix_transactions_recipient_activity_keyset"]
        )
        assert (
            "WHERE (recipient IS NOT NULL)" in before["ix_transactions_recipient_activity_keyset"]
        )

        command.downgrade(config, "0002")
        downgraded = asyncio.run(_indexes())
        assert expected.isdisjoint(downgraded)
        assert existing <= downgraded.keys()

        command.upgrade(config, "0003")
        upgraded = asyncio.run(_indexes())
        assert expected <= upgraded.keys()
        assert existing <= upgraded.keys()
    finally:
        command.upgrade(config, "head")
