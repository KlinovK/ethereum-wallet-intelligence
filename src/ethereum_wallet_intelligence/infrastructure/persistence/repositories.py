"""SQLAlchemy implementations of application repository ports."""

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ethereum_wallet_intelligence.application import (
    CheckpointConflictError,
    IndexerCheckpoint,
    PersistenceConflictError,
)
from ethereum_wallet_intelligence.domain.ethereum import (
    Block,
    BlockNumber,
    Transaction,
    TransactionHash,
)
from ethereum_wallet_intelligence.infrastructure.persistence.mappers import (
    block_to_domain,
    block_to_model,
    checkpoint_to_domain,
    checkpoint_to_model,
    transaction_to_domain,
    transaction_to_model,
)
from ethereum_wallet_intelligence.infrastructure.persistence.models import (
    BlockModel,
    IndexerCheckpointModel,
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

    async def has_any(self) -> bool:
        """Return whether any indexed block exists."""
        statement = select(BlockModel.number).limit(1)
        return (await self._session.execute(statement)).scalar_one_or_none() is not None

    async def get_highest_number(self) -> BlockNumber | None:
        """Return the highest persisted block number, if any."""
        highest = await self._session.scalar(select(func.max(BlockModel.number)))
        return BlockNumber(highest) if highest is not None else None

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

    async def delete_after(self, block_number: BlockNumber) -> None:
        """Delete blocks above a canonical ancestor within the caller's transaction."""
        await self._session.execute(
            delete(BlockModel).where(BlockModel.number > block_number.value)
        )
        await self._session.flush()


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

    async def has_any(self) -> bool:
        """Return whether any indexed transaction exists."""
        statement = select(TransactionModel.hash).limit(1)
        return (await self._session.execute(statement)).scalar_one_or_none() is not None

    async def get_by_block_number(
        self,
        block_number: BlockNumber,
    ) -> tuple[Transaction, ...]:
        """Return all persisted transactions for a block in deterministic hash order."""
        statement = (
            select(TransactionModel)
            .where(TransactionModel.block_number == block_number.value)
            .order_by(TransactionModel.hash)
        )
        models = (await self._session.scalars(statement)).all()
        return tuple(transaction_to_domain(model) for model in models)

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


class SQLAlchemyCheckpointRepository:
    """PostgreSQL-backed singleton checkpoint with pessimistic write locking."""

    _SINGLETON_ID = 1

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_update(self) -> IndexerCheckpoint | None:
        """Read and lock the checkpoint row until the surrounding transaction ends."""
        model = await self._get_locked_model()
        if model is None:
            return None
        return checkpoint_to_domain(model)

    async def create(self, checkpoint: IndexerCheckpoint) -> None:
        """Create the singleton checkpoint, rejecting concurrent initialization."""
        existing = await self._get_locked_model()
        if existing is not None:
            raise CheckpointConflictError("The indexing checkpoint already exists")

        self._session.add(checkpoint_to_model(checkpoint))
        try:
            await self._session.flush()
        except IntegrityError as error:
            raise CheckpointConflictError(
                "A concurrent writer created the indexing checkpoint"
            ) from error

    async def advance(
        self,
        expected: IndexerCheckpoint,
        block: Block,
    ) -> IndexerCheckpoint:
        """Advance exactly one block when the persisted expected state still matches."""
        model = await self._require_expected_state(expected)
        if block.number != expected.next_block_number:
            raise CheckpointConflictError("Checkpoint advancement must index the next block")
        if block.parent_hash != expected.expected_parent_hash:
            raise CheckpointConflictError(
                "Block parent hash does not match the checkpoint expectation"
            )

        persisted_block = await self._session.get(BlockModel, block.number.value)
        if persisted_block is None or block_to_domain(persisted_block) != block:
            raise CheckpointConflictError(
                "Checkpoint cannot advance until the exact block is persisted"
            )

        advanced = IndexerCheckpoint(
            chain_id=expected.chain_id,
            start_block_number=expected.start_block_number,
            next_block_number=BlockNumber(block.number.value + 1),
            expected_parent_hash=block.hash,
        )
        model.next_block_number = advanced.next_block_number.value
        model.expected_parent_hash = advanced.expected_parent_hash.value
        await self._session.flush()
        return advanced

    async def rewind(
        self,
        expected: IndexerCheckpoint,
        ancestor: Block,
    ) -> IndexerCheckpoint:
        """Move the checkpoint to the block after a retained canonical ancestor."""
        model = await self._require_expected_state(expected)
        if ancestor.number.value < expected.start_block_number.value:
            raise CheckpointConflictError("Checkpoint cannot rewind before its start boundary")
        if ancestor.number.value >= expected.next_block_number.value:
            raise CheckpointConflictError("Rewind ancestor must precede the next block")

        persisted_ancestor = await self._session.get(BlockModel, ancestor.number.value)
        if persisted_ancestor is None or block_to_domain(persisted_ancestor) != ancestor:
            raise CheckpointConflictError("Rewind ancestor is not the persisted canonical block")

        remaining_descendant = (
            await self._session.execute(
                select(BlockModel.number).where(BlockModel.number > ancestor.number.value).limit(1)
            )
        ).scalar_one_or_none()
        if remaining_descendant is not None:
            raise CheckpointConflictError(
                "Canonical descendants must be deleted before rewinding the checkpoint"
            )

        rewound = IndexerCheckpoint(
            chain_id=expected.chain_id,
            start_block_number=expected.start_block_number,
            next_block_number=BlockNumber(ancestor.number.value + 1),
            expected_parent_hash=ancestor.hash,
        )
        model.next_block_number = rewound.next_block_number.value
        model.expected_parent_hash = rewound.expected_parent_hash.value
        await self._session.flush()
        return rewound

    async def _require_expected_state(
        self,
        expected: IndexerCheckpoint,
    ) -> IndexerCheckpointModel:
        model = await self._get_locked_model()
        if model is None:
            raise CheckpointConflictError("The indexing checkpoint does not exist")
        if checkpoint_to_domain(model) != expected:
            raise CheckpointConflictError("The indexing checkpoint changed concurrently")
        return model

    async def _get_locked_model(self) -> IndexerCheckpointModel | None:
        statement = (
            select(IndexerCheckpointModel)
            .where(IndexerCheckpointModel.id == self._SINGLETON_ID)
            .with_for_update()
        )
        return (await self._session.execute(statement)).scalar_one_or_none()
