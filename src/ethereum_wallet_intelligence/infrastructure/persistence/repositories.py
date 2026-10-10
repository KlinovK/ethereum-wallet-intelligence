"""SQLAlchemy implementations of application repository ports."""

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ethereum_wallet_intelligence.application import PersistenceConflictError
from ethereum_wallet_intelligence.domain.ethereum import (
    Block,
    BlockNumber,
    Transaction,
    TransactionHash,
)
from ethereum_wallet_intelligence.infrastructure.persistence.mappers import (
    block_to_domain,
    block_to_model,
    transaction_to_domain,
    transaction_to_model,
)
from ethereum_wallet_intelligence.infrastructure.persistence.models import (
    BlockModel,
    TransactionModel,
)


class SQLAlchemyBlockRepository:
    """SQLAlchemy implementation of the BlockRepository application port."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_number(self, block_number: BlockNumber) -> Block | None:
        """Fetch a block by its number."""
        model = await self._session.get(BlockModel, block_number.value)
        if model is None:
            return None
        return block_to_domain(model)

    async def save(self, block: Block) -> None:
        """Save a block idempotently or raise PersistenceConflictError on conflict."""
        existing = await self._session.get(BlockModel, block.number.value)
        if existing is not None:
            existing_timestamp = existing.timestamp
            if existing_timestamp.tzinfo is None:
                existing_timestamp = existing_timestamp.replace(tzinfo=block.timestamp.tzinfo)
            if (
                existing.hash != block.hash.value
                or existing.parent_hash != block.parent_hash.value
                or existing_timestamp != block.timestamp
            ):
                raise PersistenceConflictError(
                    f"Conflicting block already exists for block number {block.number.value}"
                )
            return

        stmt = select(BlockModel).where(BlockModel.hash == block.hash.value)
        existing_hash = (await self._session.execute(stmt)).scalar_one_or_none()
        if existing_hash is not None and existing_hash.number != block.number.value:
            raise PersistenceConflictError(
                f"Conflicting block already exists with hash {block.hash.value}"
            )

        model = block_to_model(block)
        self._session.add(model)
        try:
            await self._session.flush()
        except IntegrityError as error:
            raise PersistenceConflictError(
                f"A database constraint prevented persisting block {block.number.value}"
            ) from error


class SQLAlchemyTransactionRepository:
    """SQLAlchemy implementation of the TransactionRepository application port."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_hash(self, transaction_hash: TransactionHash) -> Transaction | None:
        """Fetch a transaction by its hash."""
        model = await self._session.get(TransactionModel, transaction_hash.value)
        if model is None:
            return None
        return transaction_to_domain(model)

    async def save(self, transaction: Transaction) -> None:
        """Save a transaction idempotently or raise PersistenceConflictError on conflict."""
        existing = await self._session.get(TransactionModel, transaction.hash.value)
        if existing is not None:
            recipient = transaction.to.value if transaction.to is not None else None
            if (
                existing.block_number != transaction.block_number.value
                or existing.sender != transaction.sender.value
                or existing.recipient != recipient
                or int(existing.value_wei) != transaction.value.value
                or existing.nonce != transaction.nonce
                or existing.gas_limit != transaction.gas_limit
                or bytes(existing.input) != transaction.input_data
            ):
                raise PersistenceConflictError(
                    f"Conflicting transaction already exists for hash {transaction.hash.value}"
                )
            return

        block_exists = await self._session.get(BlockModel, transaction.block_number.value)
        if block_exists is None:
            raise PersistenceConflictError(
                f"Referenced block {transaction.block_number.value} does not exist "
                f"for transaction {transaction.hash.value}"
            )

        model = transaction_to_model(transaction)
        self._session.add(model)
        try:
            await self._session.flush()
        except IntegrityError as error:
            raise PersistenceConflictError(
                f"A database constraint prevented persisting transaction {transaction.hash.value}"
            ) from error
