"""Database-free tests for the PostgreSQL wallet activity reader."""

import asyncio
import logging
import traceback
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Self, cast

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Dialect, RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.schema import CreateIndex
from sqlalchemy.sql.schema import Table

from ethereum_wallet_intelligence.application import (
    StaleWalletActivityCursorError,
    WalletActivityCoverage,
    WalletActivityCursor,
    WalletActivityDirection,
    WalletActivityIndexUnavailableError,
    WalletActivityPage,
    WalletActivityQuery,
    WalletActivityReadError,
)
from ethereum_wallet_intelligence.domain.ethereum import (
    BlockHash,
    BlockNumber,
    ChainId,
    EthereumAddress,
    TransactionHash,
)
from ethereum_wallet_intelligence.infrastructure.persistence.models import TransactionModel
from ethereum_wallet_intelligence.infrastructure.persistence.wallet_activity_reader import (
    SQLAlchemyWalletActivityReader,
    _build_activity_statement,
    _map_activity_row,
    _SnapshotCoverage,
)

WALLET = EthereumAddress("0x" + "11" * 20)
OTHER = EthereumAddress("0x" + "22" * 20)
BLOCK_HASH = BlockHash("0x" + "aa" * 32)
TX_HASH = TransactionHash("0x" + "bb" * 32)
SECRET = "postgresql+asyncpg://reader:do-not-leak@localhost/wallet"


def _postgresql_dialect() -> Dialect:
    return cast(Dialect, postgresql.dialect())  # type: ignore[no-untyped-call]


def _query(
    *,
    direction: WalletActivityDirection | None = None,
    cursor: WalletActivityCursor | None = None,
    limit: int = 2,
) -> WalletActivityQuery:
    return WalletActivityQuery(
        wallet_address=WALLET,
        direction=direction,
        cursor=cursor,
        limit=limit,
    )


def _cursor(
    *,
    direction: WalletActivityDirection | None = None,
    last_block_number: int = 11,
) -> WalletActivityCursor:
    return WalletActivityCursor(
        version=1,
        wallet_address=WALLET,
        direction=direction,
        snapshot_block_number=BlockNumber(12),
        snapshot_block_hash=BLOCK_HASH,
        last_block_number=BlockNumber(last_block_number),
        last_transaction_hash=TX_HASH,
    )


def _compile(query: WalletActivityQuery) -> str:
    statement = _build_activity_statement(
        query,
        indexed_from_block=10,
        snapshot_block_number=12,
    )
    return str(
        statement.compile(
            dialect=_postgresql_dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


def _empty_page() -> WalletActivityPage:
    return WalletActivityPage(
        wallet_address=WALLET,
        direction=None,
        coverage=WalletActivityCoverage(
            chain_id=ChainId.ethereum_mainnet(),
            indexed_from_block=BlockNumber(10),
            indexed_through_block=None,
            indexed_through_hash=None,
        ),
        activities=(),
        next_cursor=None,
    )


def _row(**overrides: object) -> RowMapping:
    values: dict[str, object] = {
        "transaction_hash": TX_HASH.value,
        "block_number": 12,
        "block_hash": BLOCK_HASH.value,
        "block_timestamp": datetime(2026, 10, 20, 12, 0, tzinfo=UTC),
        "sender": OTHER.value,
        "recipient": WALLET.value,
        "value_wei": Decimal(2**256 - 1),
        "nonce": 7,
        "gas_limit": 21_000,
        "input": b"\x12\x34",
    }
    values.update(overrides)
    return cast(RowMapping, values)


def _covered_snapshot() -> _SnapshotCoverage:
    coverage = WalletActivityCoverage(
        chain_id=ChainId.ethereum_mainnet(),
        indexed_from_block=BlockNumber(10),
        indexed_through_block=BlockNumber(12),
        indexed_through_hash=BLOCK_HASH,
    )
    return _SnapshotCoverage(coverage, BlockNumber(12), BLOCK_HASH)


class _BoundaryResult:
    def __init__(self, boundary: RowMapping | None) -> None:
        self._boundary = boundary

    def mappings(self) -> Self:
        return self

    def one_or_none(self) -> RowMapping | None:
        return self._boundary


class _BoundaryConnection:
    def __init__(self, boundary: RowMapping) -> None:
        self._boundary = boundary

    async def scalar(self, statement: object) -> str:
        del statement
        return BLOCK_HASH.value

    async def execute(self, statement: object) -> _BoundaryResult:
        del statement
        return _BoundaryResult(self._boundary)


def test_all_direction_query_uses_disjoint_union_keyset_and_snapshot_bounds() -> None:
    sql = _compile(_query(cursor=_cursor()))

    assert "UNION ALL" in sql
    assert sql.count("transactions.sender =") == 1
    assert "transactions.recipient =" in sql
    assert "transactions.sender !=" in sql
    assert sql.count("transactions.block_number >= 10") == 2
    assert sql.count("transactions.block_number <= 12") == 2
    assert sql.count("transactions.block_number < 11") == 2
    assert sql.count('(transactions.hash COLLATE "C") <') == 2
    assert "ORDER BY wallet_activity_candidates.block_number DESC" in sql
    assert 'wallet_activity_candidates.transaction_hash COLLATE "C" DESC' in sql
    assert "OFFSET" not in sql
    assert "count(" not in sql.lower()


@pytest.mark.parametrize(
    ("direction", "required_sql", "forbidden_sql"),
    [
        (
            WalletActivityDirection.INCOMING,
            "transactions.sender !=",
            "IS DISTINCT FROM",
        ),
        (
            WalletActivityDirection.OUTGOING,
            "IS DISTINCT FROM",
            "transactions.sender !=",
        ),
        (
            WalletActivityDirection.SELF,
            "transactions.recipient =",
            "IS DISTINCT FROM",
        ),
    ],
)
def test_filtered_direction_query_uses_one_branch(
    direction: WalletActivityDirection,
    required_sql: str,
    forbidden_sql: str,
) -> None:
    sql = _compile(_query(direction=direction))

    assert "UNION ALL" not in sql
    assert required_sql in sql
    assert forbidden_sql not in sql
    assert "LIMIT 3" in sql


def test_model_metadata_matches_wallet_activity_indexes() -> None:
    table = cast(Table, TransactionModel.__table__)
    indexes = {str(index.name): index for index in table.indexes if index.name is not None}

    sender_sql = str(
        CreateIndex(indexes["ix_transactions_sender_activity_keyset"]).compile(
            dialect=_postgresql_dialect()
        )
    )
    recipient_sql = str(
        CreateIndex(indexes["ix_transactions_recipient_activity_keyset"]).compile(
            dialect=_postgresql_dialect()
        )
    )
    assert 'ON transactions (sender, block_number DESC, hash COLLATE "C" DESC)' in sender_sql
    assert (
        'ON transactions (recipient, block_number DESC, hash COLLATE "C" DESC) '
        "WHERE recipient IS NOT NULL" in recipient_sql
    )
    assert "ix_transactions_sender" in indexes
    assert "ix_transactions_recipient" in indexes
    assert "ix_transactions_block_number" in indexes


def test_row_mapping_preserves_exact_wei_and_classifies_activity() -> None:
    activity = _map_activity_row(_row(), _query())

    assert activity.transaction_hash == TX_HASH
    assert activity.direction is WalletActivityDirection.INCOMING
    assert activity.value.value == 2**256 - 1
    assert activity.has_input is True
    assert activity.block_timestamp.tzinfo is UTC


def test_row_mapping_supports_contract_creation_and_zero_value() -> None:
    activity = _map_activity_row(
        _row(sender=WALLET.value, recipient=None, value_wei=Decimal(0), input=b""),
        _query(direction=WalletActivityDirection.OUTGOING),
    )

    assert activity.direction is WalletActivityDirection.OUTGOING
    assert activity.is_contract_creation is True
    assert activity.value.value == 0
    assert activity.has_input is False


def test_unstarted_snapshot_still_rejects_supplied_cursor() -> None:
    class _UnstartedReader(SQLAlchemyWalletActivityReader):
        async def _load_coverage(
            self,
            connection: AsyncConnection,
        ) -> _SnapshotCoverage:
            del connection
            return _SnapshotCoverage(
                WalletActivityCoverage(
                    chain_id=ChainId.ethereum_mainnet(),
                    indexed_from_block=BlockNumber(10),
                    indexed_through_block=None,
                    indexed_through_hash=None,
                ),
                None,
                None,
            )

        async def _validate_cursor(
            self,
            connection: AsyncConnection,
            query: WalletActivityQuery,
            current: _SnapshotCoverage,
        ) -> _SnapshotCoverage:
            del connection, query, current
            raise StaleWalletActivityCursorError(
                "Wallet activity cursor snapshot is no longer indexed"
            )

    reader = _UnstartedReader(cast(AsyncEngine, object()))
    with pytest.raises(StaleWalletActivityCursorError, match="no longer indexed"):
        asyncio.run(
            reader._read_snapshot(
                cast(AsyncConnection, object()),
                _query(cursor=_cursor()),
            )
        )


def test_cursor_rejects_existing_boundary_for_another_wallet() -> None:
    connection = _BoundaryConnection(
        cast(
            RowMapping,
            {
                "block_number": 11,
                "sender": OTHER.value,
                "recipient": OTHER.value,
            },
        )
    )
    reader = SQLAlchemyWalletActivityReader(cast(AsyncEngine, object()))

    with pytest.raises(WalletActivityReadError, match="does not involve"):
        asyncio.run(
            reader._validate_cursor(
                cast(AsyncConnection, connection),
                _query(cursor=_cursor()),
                _covered_snapshot(),
            )
        )


def test_cursor_rejects_existing_boundary_with_incompatible_direction() -> None:
    direction = WalletActivityDirection.INCOMING
    connection = _BoundaryConnection(
        cast(
            RowMapping,
            {
                "block_number": 11,
                "sender": WALLET.value,
                "recipient": OTHER.value,
            },
        )
    )
    reader = SQLAlchemyWalletActivityReader(cast(AsyncEngine, object()))

    with pytest.raises(StaleWalletActivityCursorError, match="no longer matches"):
        asyncio.run(
            reader._validate_cursor(
                cast(AsyncConnection, connection),
                _query(direction=direction, cursor=_cursor(direction=direction)),
                _covered_snapshot(),
            )
        )


def test_cursor_rejects_existing_boundary_at_different_height() -> None:
    connection = _BoundaryConnection(
        cast(
            RowMapping,
            {
                "block_number": 10,
                "sender": OTHER.value,
                "recipient": WALLET.value,
            },
        )
    )
    reader = SQLAlchemyWalletActivityReader(cast(AsyncEngine, object()))

    with pytest.raises(StaleWalletActivityCursorError, match="no longer matches"):
        asyncio.run(
            reader._validate_cursor(
                cast(AsyncConnection, connection),
                _query(cursor=_cursor()),
                _covered_snapshot(),
            )
        )


def test_limit_lookahead_row_is_validated_before_page_is_returned() -> None:
    class _RowsResult:
        def mappings(self) -> Self:
            return self

        def all(self) -> list[RowMapping]:
            return [
                _row(transaction_hash="0x" + "cc" * 32),
                _row(transaction_hash=TX_HASH.value.upper().replace("0X", "0x")),
            ]

    class _RowsConnection:
        async def execute(self, statement: object) -> _RowsResult:
            del statement
            return _RowsResult()

    class _CoveredReader(SQLAlchemyWalletActivityReader):
        async def _load_coverage(
            self,
            connection: AsyncConnection,
        ) -> _SnapshotCoverage:
            del connection
            coverage = WalletActivityCoverage(
                chain_id=ChainId.ethereum_mainnet(),
                indexed_from_block=BlockNumber(10),
                indexed_through_block=BlockNumber(12),
                indexed_through_hash=BLOCK_HASH,
            )
            return _SnapshotCoverage(coverage, BlockNumber(12), BLOCK_HASH)

    reader = _CoveredReader(cast(AsyncEngine, object()))
    with pytest.raises(WalletActivityReadError, match="not canonical"):
        asyncio.run(
            reader._read_snapshot(
                cast(AsyncConnection, _RowsConnection()),
                _query(limit=1),
            )
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"value_wei": Decimal("1.5")},
        {"value_wei": Decimal("Infinity")},
        {"value_wei": 1},
        {"transaction_hash": "not-a-hash"},
        {"transaction_hash": TX_HASH.value.upper().replace("0X", "0x")},
        {"block_number": True},
        {"block_hash": "0x1234"},
        {"block_timestamp": "2026-10-20"},
        {"sender": "not-an-address"},
        {"recipient": 42},
        {"nonce": -1},
        {"gas_limit": -1},
        {"input": "0x1234"},
    ],
)
def test_row_mapping_rejects_malformed_database_values(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(WalletActivityReadError):
        _map_activity_row(_row(**overrides), _query())


class _FakeConnection:
    def __init__(self) -> None:
        self.in_transaction_value = False
        self.executed: list[str] = []
        self.begin_error: BaseException | None = None
        self.rollback_error: BaseException | None = None
        self.close_error: BaseException | None = None
        self.invalidate_error: BaseException | None = None
        self.rolled_back = False
        self.closed = False
        self.invalidated = False

    async def execution_options(self, **options: object) -> Self:
        assert options == {"isolation_level": "REPEATABLE READ"}
        return self

    async def begin(self) -> None:
        if self.begin_error is not None:
            raise self.begin_error
        self.in_transaction_value = True

    async def execute(self, statement: object, parameters: object = None) -> object:
        del parameters
        self.executed.append(str(statement))
        return object()

    def in_transaction(self) -> bool:
        return self.in_transaction_value

    async def rollback(self) -> None:
        if self.rollback_error is not None:
            raise self.rollback_error
        self.rolled_back = True
        self.in_transaction_value = False

    async def close(self) -> None:
        if self.close_error is not None:
            raise self.close_error
        self.closed = True

    async def invalidate(self) -> None:
        if self.invalidate_error is not None:
            raise self.invalidate_error
        self.invalidated = True


class _FakeEngine:
    def __init__(self, connection: _FakeConnection) -> None:
        self.connection = connection
        self.connect_error: BaseException | None = None

    async def connect(self) -> _FakeConnection:
        if self.connect_error is not None:
            raise self.connect_error
        return self.connection


class _LifecycleReader(SQLAlchemyWalletActivityReader):
    def __init__(
        self,
        engine: _FakeEngine,
        operation: Callable[[], Awaitable[WalletActivityPage]],
    ) -> None:
        super().__init__(cast(AsyncEngine, engine))
        self._operation = operation

    async def _read_snapshot(
        self,
        connection: AsyncConnection,
        query: WalletActivityQuery,
    ) -> WalletActivityPage:
        del connection, query
        return await self._operation()


def test_read_lifecycle_is_repeatable_read_read_only_and_rolls_back() -> None:
    connection = _FakeConnection()

    async def _operation() -> WalletActivityPage:
        return _empty_page()

    page = asyncio.run(_LifecycleReader(_FakeEngine(connection), _operation).read_page(_query()))

    assert page == _empty_page()
    assert connection.rolled_back is True
    assert connection.closed is True
    assert connection.invalidated is False
    assert connection.executed[0] == "SET TRANSACTION READ ONLY"
    assert "set_config" in connection.executed[1]


def test_credential_bearing_query_failure_is_sanitized(
    caplog: pytest.LogCaptureFixture,
) -> None:
    connection = _FakeConnection()

    async def _operation() -> WalletActivityPage:
        raise RuntimeError(SECRET)

    caplog.set_level(logging.DEBUG)
    caught: WalletActivityIndexUnavailableError | None = None
    try:
        asyncio.run(_LifecycleReader(_FakeEngine(connection), _operation).read_page(_query()))
    except WalletActivityIndexUnavailableError as error:
        caught = error
        rendered = "".join(traceback.format_exception(error))
    else:
        pytest.fail("Expected a credential-safe wallet activity read error")

    assert SECRET not in rendered
    assert SECRET not in caplog.text
    assert caught is not None
    assert caught.__cause__ is None
    assert caught.__context__ is None
    assert connection.rolled_back is True
    assert connection.closed is True


def test_query_cancellation_preserves_original_identity() -> None:
    connection = _FakeConnection()
    cancellation = asyncio.CancelledError("cancel wallet read")

    async def _operation() -> WalletActivityPage:
        raise cancellation

    with pytest.raises(asyncio.CancelledError) as raised:
        asyncio.run(_LifecycleReader(_FakeEngine(connection), _operation).read_page(_query()))

    assert raised.value is cancellation
    assert connection.rolled_back is True
    assert connection.closed is True


@pytest.mark.parametrize(
    "process_control",
    [
        pytest.param(KeyboardInterrupt("stop wallet read"), id="keyboard-interrupt"),
        pytest.param(SystemExit("stop wallet read"), id="system-exit"),
    ],
)
def test_process_control_during_read_preserves_original_identity(
    process_control: BaseException,
) -> None:
    connection = _FakeConnection()

    async def _operation() -> WalletActivityPage:
        raise process_control

    caught: BaseException | None = None
    try:
        asyncio.run(_LifecycleReader(_FakeEngine(connection), _operation).read_page(_query()))
    except BaseException as error:
        caught = error

    assert caught is process_control
    assert connection.rolled_back is True
    assert connection.closed is True


def test_connection_failure_is_translated_without_credentials() -> None:
    connection = _FakeConnection()
    engine = _FakeEngine(connection)
    engine.connect_error = RuntimeError(SECRET)

    async def _operation() -> WalletActivityPage:
        return _empty_page()

    with pytest.raises(WalletActivityIndexUnavailableError) as raised:
        asyncio.run(_LifecycleReader(engine, _operation).read_page(_query()))

    rendered = "".join(traceback.format_exception(raised.value))
    assert SECRET not in rendered
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_begin_cancellation_is_preserved_after_connection_cleanup() -> None:
    connection = _FakeConnection()
    cancellation = asyncio.CancelledError("cancel begin")
    connection.begin_error = cancellation

    async def _operation() -> WalletActivityPage:
        return _empty_page()

    with pytest.raises(asyncio.CancelledError) as raised:
        asyncio.run(_LifecycleReader(_FakeEngine(connection), _operation).read_page(_query()))

    assert raised.value is cancellation
    assert connection.closed is True


@pytest.mark.parametrize("failed_stage", ["rollback", "close"])
def test_cleanup_failure_invalidates_and_never_returns_page(failed_stage: str) -> None:
    connection = _FakeConnection()
    setattr(connection, f"{failed_stage}_error", RuntimeError(SECRET))

    async def _operation() -> WalletActivityPage:
        return _empty_page()

    with pytest.raises(
        WalletActivityIndexUnavailableError,
        match="cleanup required connection invalidation",
    ) as raised:
        asyncio.run(_LifecycleReader(_FakeEngine(connection), _operation).read_page(_query()))

    assert SECRET not in "".join(traceback.format_exception(raised.value))
    assert connection.invalidated is True


def test_unconfirmed_invalidation_failure_fails_closed() -> None:
    connection = _FakeConnection()
    connection.close_error = RuntimeError(SECRET)
    connection.invalidate_error = RuntimeError(SECRET)

    async def _operation() -> WalletActivityPage:
        return _empty_page()

    with pytest.raises(
        WalletActivityIndexUnavailableError,
        match="cleanup could not be confirmed",
    ) as raised:
        asyncio.run(_LifecycleReader(_FakeEngine(connection), _operation).read_page(_query()))

    assert SECRET not in "".join(traceback.format_exception(raised.value))


def test_cancellation_during_cleanup_propagates() -> None:
    connection = _FakeConnection()
    cancellation = asyncio.CancelledError("cancel rollback")
    connection.rollback_error = cancellation

    async def _operation() -> WalletActivityPage:
        return _empty_page()

    with pytest.raises(asyncio.CancelledError) as raised:
        asyncio.run(_LifecycleReader(_FakeEngine(connection), _operation).read_page(_query()))

    assert raised.value is cancellation
    assert connection.invalidated is True


@pytest.mark.parametrize(
    ("process_control", "failed_stage"),
    [
        pytest.param(
            KeyboardInterrupt("interrupt rollback"),
            "rollback",
            id="keyboard-interrupt-rollback",
        ),
        pytest.param(SystemExit("interrupt close"), "close", id="system-exit-close"),
    ],
)
def test_process_control_during_cleanup_propagates(
    process_control: BaseException,
    failed_stage: str,
) -> None:
    connection = _FakeConnection()
    setattr(connection, f"{failed_stage}_error", process_control)

    async def _operation() -> WalletActivityPage:
        return _empty_page()

    caught: BaseException | None = None
    try:
        asyncio.run(_LifecycleReader(_FakeEngine(connection), _operation).read_page(_query()))
    except BaseException as error:
        caught = error

    assert caught is process_control
    assert connection.invalidated is True


def test_original_process_control_precedes_cleanup_cancellation() -> None:
    connection = _FakeConnection()
    primary = KeyboardInterrupt("primary interrupt")
    connection.rollback_error = asyncio.CancelledError("cleanup cancellation")

    async def _operation() -> WalletActivityPage:
        raise primary

    caught: BaseException | None = None
    try:
        asyncio.run(_LifecycleReader(_FakeEngine(connection), _operation).read_page(_query()))
    except BaseException as error:
        caught = error

    assert caught is primary
    assert connection.invalidated is True


@pytest.mark.parametrize("stage", ["close", "invalidate"])
def test_cancellation_during_later_cleanup_stage_propagates(stage: str) -> None:
    connection = _FakeConnection()
    cancellation = asyncio.CancelledError(f"cancel {stage}")
    if stage == "close":
        connection.close_error = cancellation
    else:
        connection.close_error = RuntimeError(SECRET)
        connection.invalidate_error = cancellation

    async def _operation() -> WalletActivityPage:
        return _empty_page()

    with pytest.raises(asyncio.CancelledError) as raised:
        asyncio.run(_LifecycleReader(_FakeEngine(connection), _operation).read_page(_query()))

    assert raised.value is cancellation


def test_primary_cancellation_survives_cleanup_recovery() -> None:
    connection = _FakeConnection()
    cancellation = asyncio.CancelledError("cancel query")
    connection.close_error = RuntimeError(SECRET)

    async def _operation() -> WalletActivityPage:
        raise cancellation

    with pytest.raises(asyncio.CancelledError) as raised:
        asyncio.run(_LifecycleReader(_FakeEngine(connection), _operation).read_page(_query()))

    assert raised.value is cancellation
    assert connection.invalidated is True


@pytest.mark.parametrize(
    ("process_control", "failed_stage"),
    [
        pytest.param(
            KeyboardInterrupt("stop after rollback failure"),
            "rollback",
            id="keyboard-interrupt-rollback",
        ),
        pytest.param(
            SystemExit("stop after close failure"),
            "close",
            id="system-exit-close",
        ),
    ],
)
def test_primary_process_control_survives_cleanup_recovery(
    process_control: BaseException,
    failed_stage: str,
) -> None:
    connection = _FakeConnection()
    setattr(connection, f"{failed_stage}_error", RuntimeError(SECRET))

    async def _operation() -> WalletActivityPage:
        raise process_control

    caught: BaseException | None = None
    try:
        asyncio.run(_LifecycleReader(_FakeEngine(connection), _operation).read_page(_query()))
    except BaseException as error:
        caught = error

    assert caught is process_control
    assert connection.invalidated is True


@pytest.mark.parametrize(
    ("process_control", "failed_stage", "expected_stage"),
    [
        pytest.param(
            KeyboardInterrupt("stop after failed rollback disposal"),
            "rollback",
            "rollback_failed",
            id="keyboard-interrupt-rollback",
        ),
        pytest.param(
            SystemExit("stop after failed close disposal"),
            "close",
            "close_failed",
            id="system-exit-close",
        ),
    ],
)
def test_primary_process_control_survives_unconfirmed_cleanup(
    process_control: BaseException,
    failed_stage: str,
    expected_stage: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    connection = _FakeConnection()
    setattr(connection, f"{failed_stage}_error", RuntimeError(SECRET))
    connection.invalidate_error = RuntimeError(SECRET)

    async def _operation() -> WalletActivityPage:
        raise process_control

    caught: BaseException | None = None
    with caplog.at_level(
        logging.WARNING,
        logger=("ethereum_wallet_intelligence.infrastructure.persistence.wallet_activity_reader"),
    ):
        try:
            asyncio.run(_LifecycleReader(_FakeEngine(connection), _operation).read_page(_query()))
        except BaseException as error:
            caught = error

    assert caught is process_control
    rendered = "".join(traceback.TracebackException.from_exception(caught).format(chain=True))
    assert SECRET not in rendered
    assert SECRET not in caplog.text
    assert caught.__cause__ is None
    assert caught.__context__ is None
    assert getattr(caught, "__notes__", []) == [
        "Wallet activity database cleanup could not be confirmed "
        f"(stage={expected_stage}, invalidation_stage=invalidation_failed)"
    ]
    unconfirmed = next(
        record
        for record in caplog.records
        if getattr(record, "event", None) == "wallet_activity_cleanup_unconfirmed"
    )
    assert unconfirmed.__dict__["stage"] == expected_stage
    assert unconfirmed.__dict__["invalidation_stage"] == "invalidation_failed"


def test_primary_cancellation_survives_unconfirmed_cleanup() -> None:
    connection = _FakeConnection()
    cancellation = asyncio.CancelledError("cancel query")
    connection.close_error = RuntimeError(SECRET)
    connection.invalidate_error = RuntimeError(SECRET)

    async def _operation() -> WalletActivityPage:
        raise cancellation

    with pytest.raises(asyncio.CancelledError) as raised:
        asyncio.run(_LifecycleReader(_FakeEngine(connection), _operation).read_page(_query()))

    assert raised.value is cancellation
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert getattr(raised.value, "__notes__", []) == [
        "Wallet activity database cleanup could not be confirmed "
        "(stage=close_failed, invalidation_stage=invalidation_failed)"
    ]
    assert SECRET not in "".join(
        traceback.TracebackException.from_exception(raised.value).format(chain=True)
    )


@pytest.mark.parametrize("timeout", [True, 0, -1, 30_001, 1.5])
def test_statement_timeout_is_bounded(timeout: object) -> None:
    with pytest.raises(ValueError, match="statement timeout"):
        SQLAlchemyWalletActivityReader(
            cast(AsyncEngine, _FakeEngine(_FakeConnection())),
            statement_timeout_ms=cast(int, timeout),
        )
