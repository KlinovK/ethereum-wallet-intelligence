"""add indexer checkpoint and cascading block deletion

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-12 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "indexer_checkpoint",
        sa.Column("id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("chain_id", sa.BigInteger(), nullable=False),
        sa.Column("start_block_number", sa.BigInteger(), nullable=False),
        sa.Column("next_block_number", sa.BigInteger(), nullable=False),
        sa.Column("expected_parent_hash", sa.String(length=66), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_indexer_checkpoint_singleton"),
        sa.CheckConstraint(
            "chain_id > 0",
            name="ck_indexer_checkpoint_chain_id_positive",
        ),
        sa.CheckConstraint(
            "start_block_number >= 0",
            name="ck_indexer_checkpoint_start_non_negative",
        ),
        sa.CheckConstraint(
            "next_block_number >= start_block_number",
            name="ck_indexer_checkpoint_next_not_before_start",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.drop_constraint(
        "transactions_block_number_fkey",
        "transactions",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "transactions_block_number_fkey",
        "transactions",
        "blocks",
        ["block_number"],
        ["number"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "transactions_block_number_fkey",
        "transactions",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "transactions_block_number_fkey",
        "transactions",
        "blocks",
        ["block_number"],
        ["number"],
    )
    op.drop_table("indexer_checkpoint")
