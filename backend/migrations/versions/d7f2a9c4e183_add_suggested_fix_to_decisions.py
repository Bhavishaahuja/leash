"""add suggested_fix to decisions

Revision ID: d7f2a9c4e183
Revises: c3e8d1a5f962
Create Date: 2026-11-10 14:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d7f2a9c4e183"
down_revision: Union[str, Sequence[str], None] = "c3e8d1a5f962"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # null on every existing row: those were decided without a suggestion, and making one up now
    # from today's catalog would put something in the record that nobody saw at the time
    with op.batch_alter_table("decisions") as batch_op:
        batch_op.add_column(sa.Column("suggested_fix", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("decisions") as batch_op:
        batch_op.drop_column("suggested_fix")
