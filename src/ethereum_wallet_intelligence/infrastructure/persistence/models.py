from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase):
    """Declarative base class for persistence entities."""


class WeiNumeric(TypeDecorator[Decimal]):
    """Exact 78-digit integer for Ethereum uint256 wei values."""

    impl = Numeric(precision=78, scale=0, asdecimal=True)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> Any:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(78))
        return dialect.type_descriptor(Numeric(precision=78, scale=0, asdecimal=True))

    def process_bind_param(self, value: Decimal | int | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        if dialect.name == "sqlite":
            return str(value)
        return Decimal(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        return Decimal(str(value))


class BlockModel(Base):
    """Database model for indexed Ethereum blocks."""

    __tablename__ = "blocks"

    number: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    hash: Mapped[str] = mapped_column(String(66), unique=True, nullable=False, index=True)
    parent_hash: Mapped[str] = mapped_column(String(66), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TransactionModel(Base):
    """Database model for indexed Ethereum transactions."""

    __tablename__ = "transactions"

    hash: Mapped[str] = mapped_column(String(66), primary_key=True)
    block_number: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("blocks.number", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sender: Mapped[str] = mapped_column(String(42), nullable=False, index=True)
    recipient: Mapped[str | None] = mapped_column(String(42), nullable=True, index=True)
    value_wei: Mapped[Decimal] = mapped_column(WeiNumeric, nullable=False)
    nonce: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gas_limit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    input: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


class IndexerCheckpointModel(Base):
    """Singleton database state for a blockchain indexing stream."""

    __tablename__ = "indexer_checkpoint"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_indexer_checkpoint_singleton"),
        CheckConstraint("chain_id > 0", name="ck_indexer_checkpoint_chain_id_positive"),
        CheckConstraint(
            "start_block_number >= 0",
            name="ck_indexer_checkpoint_start_non_negative",
        ),
        CheckConstraint(
            "next_block_number >= start_block_number",
            name="ck_indexer_checkpoint_next_not_before_start",
        ),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False, default=1)
    chain_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    start_block_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    next_block_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_parent_hash: Mapped[str] = mapped_column(String(66), nullable=False)
