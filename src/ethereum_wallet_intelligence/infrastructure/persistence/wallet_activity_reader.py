"""PostgreSQL reader for one snapshot-consistent wallet activity page."""

from __future__ import annotations

import logging
from asyncio import CancelledError
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Select, and_, exists, func, or_, select, text, union_all
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.elements import ColumnElement

from ethereum_wallet_intelligence.application import (
    StaleWalletActivityCursorError,
    WalletActivityCoverage,
    WalletActivityCursor,
    WalletActivityDirection,
    WalletActivityIndexUnavailableError,
    WalletActivityPage,
    WalletActivityQuery,
    WalletActivityReadError,
    WalletTransactionActivity,
    classify_wallet_activity_direction,
)
from ethereum_wallet_intelligence.domain.ethereum import (
    BlockHash,
    BlockNumber,
    ChainId,
    DomainValidationError,
    EthereumAddress,
    TransactionHash,
    Wei,
)
from ethereum_wallet_intelligence.infrastructure.persistence.models import (
    BlockModel,
    IndexerCheckpointModel,
    TransactionModel,
)

_logger = logging.getLogger(__name__)
_CHECKPOINT_ID = 1
_GENESIS_PARENT_HASH = "0x" + "00" * 32
_DEFAULT_STATEMENT_TIMEOUT_MS = 2_000
_MAX_STATEMENT_TIMEOUT_MS = 30_000


@dataclass(frozen=True, slots=True)
class _SnapshotCoverage:
    coverage: WalletActivityCoverage
    snapshot_block_number: BlockNumber | None
    snapshot_block_hash: BlockHash | None


async def _dispose_read_connection(
    connection: AsyncConnection,
    *,
    process_control: BaseException | None,
) -> tuple[bool, str | None, BaseException | None]:
    """Release or invalidate a connection while retaining process control."""
    cleanup_stage: str | None = None
    invalidation_stage: str | None = None
    disposal_confirmed = False

    try:
        if connection.in_transaction():
            await connection.rollback()
    except CancelledError as error:
        if process_control is None:
            process_control = error
        cleanup_stage = "rollback_cancelled"
    except Exception:
        cleanup_stage = "rollback_failed"
    except BaseException as error:
        if process_control is None:
            process_control = error
        cleanup_stage = "rollback_interrupted"

    if cleanup_stage is None:
        try:
            await connection.close()
            disposal_confirmed = True
        except CancelledError as error:
            if process_control is None:
                process_control = error
            cleanup_stage = "close_cancelled"
        except Exception:
            cleanup_stage = "close_failed"
        except BaseException as error:
            if process_control is None:
                process_control = error
            cleanup_stage = "close_interrupted"

    if cleanup_stage is not None:
        _logger.warning(
            "wallet_activity_cleanup_recovery_started",
            extra={
                "event": "wallet_activity_cleanup_recovery_started",
                "stage": cleanup_stage,
            },
        )
        try:
            await connection.invalidate()
            disposal_confirmed = True
            invalidation_stage = "invalidation_succeeded"
        except CancelledError as error:
            if process_control is None:
                process_control = error
            invalidation_stage = "invalidation_cancelled"
        except Exception:
            invalidation_stage = "invalidation_failed"
        except BaseException as error:
            if process_control is None:
                process_control = error
            invalidation_stage = "invalidation_interrupted"

    if not disposal_confirmed:
        safe_invalidation_stage = invalidation_stage or "invalidation_not_attempted"
        _logger.error(
            "wallet_activity_cleanup_unconfirmed",
            extra={
                "event": "wallet_activity_cleanup_unconfirmed",
                "stage": cleanup_stage or "cleanup_unknown",
                "invalidation_stage": safe_invalidation_stage,
            },
        )
        if process_control is not None:
            process_control.add_note(
                "Wallet activity database cleanup could not be confirmed "
                f"(stage={cleanup_stage or 'cleanup_unknown'}, "
                f"invalidation_stage={safe_invalidation_stage})"
            )
    elif cleanup_stage is not None:
        _logger.warning(
            "wallet_activity_cleanup_recovered",
            extra={
                "event": "wallet_activity_cleanup_recovered",
                "stage": cleanup_stage,
                "invalidation_stage": invalidation_stage,
            },
        )

    return disposal_confirmed, cleanup_stage, process_control


class SQLAlchemyWalletActivityReader:
    """Read indexed wallet activity from one PostgreSQL snapshot."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        statement_timeout_ms: int = _DEFAULT_STATEMENT_TIMEOUT_MS,
    ) -> None:
        if type(statement_timeout_ms) is not int or not (
            1 <= statement_timeout_ms <= _MAX_STATEMENT_TIMEOUT_MS
        ):
            raise ValueError(
                "Wallet activity statement timeout must be an integer from 1 through 30000"
            )
        self._engine = engine
        self._statement_timeout_ms = statement_timeout_ms

    async def read_page(self, query: WalletActivityQuery) -> WalletActivityPage:
        """Return one page and only after its read transaction is safely disposed."""
        connection: AsyncConnection | None = None
        page: WalletActivityPage | None = None
        primary_error: BaseException | None = None

        try:
            connection = await self._engine.connect()
            connection = await connection.execution_options(isolation_level="REPEATABLE READ")
            await connection.begin()
            await connection.execute(text("SET TRANSACTION READ ONLY"))
            await connection.execute(
                text("SELECT set_config('statement_timeout', :timeout, true)"),
                {"timeout": f"{self._statement_timeout_ms}ms"},
            )
            page = await self._read_snapshot(connection, query)
        except CancelledError as error:
            primary_error = error
        except WalletActivityReadError as error:
            primary_error = error
        except Exception:
            primary_error = WalletActivityIndexUnavailableError(
                "Wallet activity database read failed safely"
            )
        except BaseException as error:
            # Process-level signals are retained only so cleanup can run before re-raising.
            primary_error = error

        if connection is None:
            if primary_error is not None:
                raise primary_error
            raise WalletActivityIndexUnavailableError(
                "Wallet activity database connection was not established"
            )

        primary_process_control = (
            primary_error
            if primary_error is not None and not isinstance(primary_error, Exception)
            else None
        )
        disposal_confirmed, cleanup_stage, process_control = await _dispose_read_connection(
            connection,
            process_control=primary_process_control,
        )
        if process_control is not None:
            raise process_control
        if not disposal_confirmed:
            raise WalletActivityIndexUnavailableError(
                "Wallet activity database cleanup could not be confirmed"
            )
        if cleanup_stage is not None and primary_error is None:
            raise WalletActivityIndexUnavailableError(
                "Wallet activity database cleanup required connection invalidation"
            )
        if primary_error is not None:
            raise primary_error
        if page is None:
            raise WalletActivityReadError("Wallet activity read produced no result")
        return page

    async def _read_snapshot(
        self,
        connection: AsyncConnection,
        query: WalletActivityQuery,
    ) -> WalletActivityPage:
        current = await self._load_coverage(connection)
        if query.cursor is not None:
            snapshot = await self._validate_cursor(connection, query, current)
        else:
            snapshot = current

        if current.snapshot_block_number is None:
            return WalletActivityPage(
                wallet_address=query.wallet_address,
                direction=query.direction,
                coverage=current.coverage,
                activities=(),
                next_cursor=None,
            )

        assert snapshot.snapshot_block_number is not None
        assert snapshot.snapshot_block_hash is not None
        statement = _build_activity_statement(
            query,
            indexed_from_block=snapshot.coverage.indexed_from_block.value,
            snapshot_block_number=snapshot.snapshot_block_number.value,
        )
        rows = (await connection.execute(statement)).mappings().all()
        mapped_rows = tuple(_map_activity_row(row, query) for row in rows)
        activities = mapped_rows[: query.limit]
        next_cursor = _make_next_cursor(
            query,
            snapshot,
            activities,
            has_more=len(mapped_rows) > query.limit,
        )
        return WalletActivityPage(
            wallet_address=query.wallet_address,
            direction=query.direction,
            coverage=snapshot.coverage,
            activities=activities,
            next_cursor=next_cursor,
        )

    async def _load_coverage(self, connection: AsyncConnection) -> _SnapshotCoverage:
        checkpoint_row = (
            (
                await connection.execute(
                    select(
                        IndexerCheckpointModel.chain_id,
                        IndexerCheckpointModel.start_block_number,
                        IndexerCheckpointModel.next_block_number,
                        IndexerCheckpointModel.expected_parent_hash,
                    ).where(IndexerCheckpointModel.id == _CHECKPOINT_ID)
                )
            )
            .mappings()
            .one_or_none()
        )

        if checkpoint_row is None:
            has_blocks, has_transactions = (
                await connection.execute(
                    select(
                        exists().where(BlockModel.number.is_not(None)),
                        exists().where(TransactionModel.hash.is_not(None)),
                    )
                )
            ).one()
            if bool(has_blocks) or bool(has_transactions):
                raise WalletActivityReadError(
                    "Indexed blockchain data exists without an indexing checkpoint"
                )
            raise WalletActivityIndexUnavailableError(
                "Wallet activity index has not been initialized"
            )

        chain_id = _integer_field(checkpoint_row, "chain_id")
        start = _integer_field(checkpoint_row, "start_block_number")
        next_block = _integer_field(checkpoint_row, "next_block_number")
        parent_hash = _string_field(checkpoint_row, "expected_parent_hash")
        try:
            chain = ChainId(chain_id)
            start_number = BlockNumber(start)
            next_number = BlockNumber(next_block)
            expected_parent = BlockHash(parent_hash)
        except DomainValidationError:
            raise WalletActivityReadError("Index checkpoint contains invalid values") from None

        if chain != ChainId.ethereum_mainnet():
            raise WalletActivityReadError("Wallet activity index is not Ethereum Mainnet")
        if expected_parent.value != parent_hash:
            raise WalletActivityReadError("Index checkpoint parent hash is not canonical")
        if next_number.value < start_number.value:
            raise WalletActivityReadError("Index checkpoint range is invalid")

        range_row = (
            await connection.execute(
                select(
                    func.min(BlockModel.number),
                    func.max(BlockModel.number),
                    exists().where(TransactionModel.hash.is_not(None)),
                )
            )
        ).one()
        minimum: object = range_row[0]
        maximum: object = range_row[1]
        range_has_transactions: object = range_row[2]

        if next_number == start_number:
            if minimum is not None or maximum is not None or bool(range_has_transactions):
                raise WalletActivityReadError(
                    "Unstarted wallet activity index contains persisted data"
                )
            if start_number.value == 0 and expected_parent.value != _GENESIS_PARENT_HASH:
                raise WalletActivityReadError("Genesis checkpoint parent hash is invalid")
            coverage = WalletActivityCoverage(
                chain_id=chain,
                indexed_from_block=start_number,
                indexed_through_block=None,
                indexed_through_hash=None,
            )
            return _SnapshotCoverage(coverage, None, None)

        tip_number = BlockNumber(next_number.value - 1)
        if minimum != start_number.value or maximum != tip_number.value:
            raise WalletActivityReadError("Persisted blocks do not match checkpoint coverage")
        tip_hash = await connection.scalar(
            select(BlockModel.hash).where(BlockModel.number == tip_number.value)
        )
        if not isinstance(tip_hash, str):
            raise WalletActivityReadError("Persisted checkpoint tip is missing")
        try:
            canonical_tip_hash = BlockHash(tip_hash)
        except DomainValidationError:
            raise WalletActivityReadError("Persisted checkpoint tip hash is invalid") from None
        if canonical_tip_hash.value != tip_hash:
            raise WalletActivityReadError("Persisted checkpoint tip hash is not canonical")
        if canonical_tip_hash != expected_parent:
            raise WalletActivityReadError("Persisted checkpoint tip hash does not match checkpoint")

        coverage = WalletActivityCoverage(
            chain_id=chain,
            indexed_from_block=start_number,
            indexed_through_block=tip_number,
            indexed_through_hash=canonical_tip_hash,
        )
        return _SnapshotCoverage(coverage, tip_number, canonical_tip_hash)

    async def _validate_cursor(
        self,
        connection: AsyncConnection,
        query: WalletActivityQuery,
        current: _SnapshotCoverage,
    ) -> _SnapshotCoverage:
        cursor = query.cursor
        assert cursor is not None
        current_tip = current.snapshot_block_number
        if (
            current_tip is None
            or cursor.snapshot_block_number.value < current.coverage.indexed_from_block.value
            or cursor.snapshot_block_number.value > current_tip.value
        ):
            raise StaleWalletActivityCursorError(
                "Wallet activity cursor snapshot is no longer indexed"
            )

        anchor_hash = await connection.scalar(
            select(BlockModel.hash).where(BlockModel.number == cursor.snapshot_block_number.value)
        )
        if anchor_hash is None:
            raise WalletActivityReadError(
                "Wallet activity cursor anchor is missing from persisted coverage"
            )
        if not isinstance(anchor_hash, str):
            raise WalletActivityReadError(
                "Wallet activity cursor anchor has an invalid database type"
            )
        try:
            canonical_anchor_hash = BlockHash(anchor_hash)
        except DomainValidationError:
            raise WalletActivityReadError("Wallet activity cursor anchor hash is invalid") from None
        if canonical_anchor_hash.value != anchor_hash:
            raise WalletActivityReadError("Wallet activity cursor anchor hash is not canonical")
        if canonical_anchor_hash != cursor.snapshot_block_hash:
            raise StaleWalletActivityCursorError("Wallet activity cursor snapshot was replaced")

        boundary = (
            (
                await connection.execute(
                    select(
                        TransactionModel.block_number,
                        TransactionModel.sender,
                        TransactionModel.recipient,
                    ).where(TransactionModel.hash == cursor.last_transaction_hash.value)
                )
            )
            .mappings()
            .one_or_none()
        )
        if boundary is None:
            raise StaleWalletActivityCursorError(
                "Wallet activity cursor boundary transaction no longer exists"
            )

        try:
            boundary_block = BlockNumber(_integer_field(boundary, "block_number"))
            sender = EthereumAddress(_string_field(boundary, "sender"))
            raw_recipient = boundary["recipient"]
            if raw_recipient is not None and not isinstance(raw_recipient, str):
                raise WalletActivityReadError(
                    "Cursor boundary recipient has an invalid database type"
                )
            recipient = EthereumAddress(raw_recipient) if raw_recipient is not None else None
            if sender.value != boundary["sender"] or (
                recipient is not None and recipient.value != raw_recipient
            ):
                raise WalletActivityReadError("Cursor boundary addresses are not canonical")
            direction = classify_wallet_activity_direction(
                query.wallet_address,
                sender,
                recipient,
            )
        except DomainValidationError:
            raise WalletActivityReadError(
                "Cursor boundary transaction contains invalid values"
            ) from None

        if (
            boundary_block != cursor.last_block_number
            or boundary_block.value < current.coverage.indexed_from_block.value
            or boundary_block.value > cursor.snapshot_block_number.value
            or (query.direction is not None and direction != query.direction)
        ):
            raise StaleWalletActivityCursorError(
                "Wallet activity cursor boundary no longer matches the query"
            )

        coverage = WalletActivityCoverage(
            chain_id=current.coverage.chain_id,
            indexed_from_block=current.coverage.indexed_from_block,
            indexed_through_block=cursor.snapshot_block_number,
            indexed_through_hash=cursor.snapshot_block_hash,
        )
        return _SnapshotCoverage(
            coverage,
            cursor.snapshot_block_number,
            cursor.snapshot_block_hash,
        )


def _activity_branch(
    query: WalletActivityQuery,
    *,
    predicate: ColumnElement[bool],
    indexed_from_block: int,
    snapshot_block_number: int,
) -> Select[str, int, str, str | None, Decimal, int, int, bytes]:
    conditions = [
        predicate,
        TransactionModel.block_number >= indexed_from_block,
        TransactionModel.block_number <= snapshot_block_number,
    ]
    if query.cursor is not None:
        cursor = query.cursor
        conditions.append(
            or_(
                TransactionModel.block_number < cursor.last_block_number.value,
                and_(
                    TransactionModel.block_number == cursor.last_block_number.value,
                    TransactionModel.hash.collate("C") < cursor.last_transaction_hash.value,
                ),
            )
        )

    return (
        select(
            TransactionModel.hash.label("transaction_hash"),
            TransactionModel.block_number,
            TransactionModel.sender,
            TransactionModel.recipient,
            TransactionModel.value_wei,
            TransactionModel.nonce,
            TransactionModel.gas_limit,
            TransactionModel.input,
        )
        .where(*conditions)
        .order_by(
            TransactionModel.block_number.desc(),
            TransactionModel.hash.collate("C").desc(),
        )
        .limit(query.limit + 1)
    )


def _build_activity_statement(
    query: WalletActivityQuery,
    *,
    indexed_from_block: int,
    snapshot_block_number: int,
) -> Select[str, int, str, datetime, str, str | None, Decimal, int, int, bytes]:
    wallet = query.wallet_address.value
    if query.direction is WalletActivityDirection.INCOMING:
        predicates = [and_(TransactionModel.recipient == wallet, TransactionModel.sender != wallet)]
    elif query.direction is WalletActivityDirection.OUTGOING:
        predicates = [
            and_(
                TransactionModel.sender == wallet,
                TransactionModel.recipient.is_distinct_from(wallet),
            )
        ]
    elif query.direction is WalletActivityDirection.SELF:
        predicates = [and_(TransactionModel.sender == wallet, TransactionModel.recipient == wallet)]
    else:
        predicates = [
            TransactionModel.sender == wallet,
            and_(TransactionModel.recipient == wallet, TransactionModel.sender != wallet),
        ]

    branches = [
        _activity_branch(
            query,
            predicate=predicate,
            indexed_from_block=indexed_from_block,
            snapshot_block_number=snapshot_block_number,
        )
        for predicate in predicates
    ]
    candidates = union_all(*branches).subquery("wallet_activity_candidates")
    return (
        select(
            candidates.c.transaction_hash,
            candidates.c.block_number,
            BlockModel.hash.label("block_hash"),
            BlockModel.timestamp.label("block_timestamp"),
            candidates.c.sender,
            candidates.c.recipient,
            candidates.c.value_wei,
            candidates.c.nonce,
            candidates.c.gas_limit,
            candidates.c.input,
        )
        .join(BlockModel, BlockModel.number == candidates.c.block_number)
        .order_by(
            candidates.c.block_number.desc(),
            candidates.c.transaction_hash.collate("C").desc(),
        )
        .limit(query.limit + 1)
    )


def _map_activity_row(
    row: RowMapping,
    query: WalletActivityQuery,
) -> WalletTransactionActivity:
    raw_value = row["value_wei"]
    if (
        not isinstance(raw_value, Decimal)
        or not raw_value.is_finite()
        or raw_value != raw_value.to_integral_value()
    ):
        raise WalletActivityReadError("Wallet activity value is not an exact integer")
    raw_timestamp = row["block_timestamp"]
    raw_input = row["input"]
    raw_recipient = row["recipient"]
    if not isinstance(raw_timestamp, datetime):
        raise WalletActivityReadError("Wallet activity timestamp has an invalid database type")
    if not isinstance(raw_input, bytes):
        raise WalletActivityReadError("Wallet activity input has an invalid database type")
    if raw_recipient is not None and not isinstance(raw_recipient, str):
        raise WalletActivityReadError("Wallet activity recipient has an invalid database type")

    try:
        raw_transaction_hash = _string_field(row, "transaction_hash")
        raw_block_hash = _string_field(row, "block_hash")
        raw_sender = _string_field(row, "sender")
        transaction_hash = TransactionHash(raw_transaction_hash)
        block_hash = BlockHash(raw_block_hash)
        sender = EthereumAddress(raw_sender)
        recipient = EthereumAddress(raw_recipient) if raw_recipient is not None else None
        if (
            transaction_hash.value != raw_transaction_hash
            or block_hash.value != raw_block_hash
            or sender.value != raw_sender
            or (recipient is not None and recipient.value != raw_recipient)
        ):
            raise WalletActivityReadError("Wallet activity row is not canonical")
        direction = classify_wallet_activity_direction(
            query.wallet_address,
            sender,
            recipient,
        )
        activity = WalletTransactionActivity(
            transaction_hash=transaction_hash,
            block_number=BlockNumber(_integer_field(row, "block_number")),
            block_hash=block_hash,
            block_timestamp=raw_timestamp,
            direction=direction,
            sender=sender,
            recipient=recipient,
            value=Wei(int(raw_value)),
            nonce=_integer_field(row, "nonce"),
            gas_limit=_integer_field(row, "gas_limit"),
            has_input=bool(raw_input),
        )
    except DomainValidationError:
        raise WalletActivityReadError("Wallet activity row contains invalid values") from None
    if query.direction is not None and activity.direction != query.direction:
        raise WalletActivityReadError("Wallet activity row does not match its query filter")
    return activity


def _make_next_cursor(
    query: WalletActivityQuery,
    snapshot: _SnapshotCoverage,
    activities: tuple[WalletTransactionActivity, ...],
    *,
    has_more: bool,
) -> WalletActivityCursor | None:
    if not has_more or not activities:
        return None
    assert snapshot.snapshot_block_number is not None
    assert snapshot.snapshot_block_hash is not None
    last = activities[-1]
    return WalletActivityCursor(
        version=1,
        wallet_address=query.wallet_address,
        direction=query.direction,
        snapshot_block_number=snapshot.snapshot_block_number,
        snapshot_block_hash=snapshot.snapshot_block_hash,
        last_block_number=last.block_number,
        last_transaction_hash=last.transaction_hash,
    )


def _integer_field(row: RowMapping, name: str) -> int:
    value = row[name]
    if type(value) is not int:
        raise WalletActivityReadError(f"{name} has an invalid database type")
    return value


def _string_field(row: RowMapping, name: str) -> str:
    value = row[name]
    if not isinstance(value, str):
        raise WalletActivityReadError(f"{name} has an invalid database type")
    return value
