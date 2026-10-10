"""create blocks and transactions tables

Revision ID: 0001
Revises:
Create Date: 2026-09-29 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "blocks",
        sa.Column("number", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("hash", sa.String(length=66), nullable=False),
        sa.Column("parent_hash", sa.String(length=66), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("number"),
    )
    op.create_index(op.f("ix_blocks_hash"), "blocks", ["hash"], unique=True)

    op.create_table(
        "transactions",
        sa.Column("hash", sa.String(length=66), nullable=False),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        sa.Column("sender", sa.String(length=42), nullable=False),
        sa.Column("recipient", sa.String(length=42), nullable=True),
        sa.Column("value_wei", sa.Numeric(precision=78, scale=0), nullable=False),
        sa.Column("nonce", sa.BigInteger(), nullable=False),
        sa.Column("gas_limit", sa.BigInteger(), nullable=False),
        sa.Column("input", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(["block_number"], ["blocks.number"]),
        sa.PrimaryKeyConstraint("hash"),
    )
    op.create_index(
        op.f("ix_transactions_block_number"), "transactions", ["block_number"], unique=False
    )
    op.create_index(op.f("ix_transactions_sender"), "transactions", ["sender"], unique=False)
    op.create_index(op.f("ix_transactions_recipient"), "transactions", ["recipient"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_transactions_recipient"), table_name="transactions")
    op.drop_index(op.f("ix_transactions_sender"), table_name="transactions")
    op.drop_index(op.f("ix_transactions_block_number"), table_name="transactions")
    op.drop_table("transactions")
    op.drop_index(op.f("ix_blocks_hash"), table_name="blocks")
    op.drop_table("blocks")
