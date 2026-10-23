"""add wallet activity keyset indexes

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-20 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_transactions_sender_activity_keyset",
        "transactions",
        [
            "sender",
            sa.text("block_number DESC"),
            sa.text('hash COLLATE "C" DESC'),
        ],
        unique=False,
    )
    op.create_index(
        "ix_transactions_recipient_activity_keyset",
        "transactions",
        [
            "recipient",
            sa.text("block_number DESC"),
            sa.text('hash COLLATE "C" DESC'),
        ],
        unique=False,
        postgresql_where=sa.text("recipient IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_transactions_recipient_activity_keyset",
        table_name="transactions",
    )
    op.drop_index(
        "ix_transactions_sender_activity_keyset",
        table_name="transactions",
    )
