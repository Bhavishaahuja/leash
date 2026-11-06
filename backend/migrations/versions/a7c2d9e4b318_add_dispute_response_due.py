"""add seller_response_due_at to disputes

Revision ID: a7c2d9e4b318
Revises: e3a9f1c47d25
Create Date: 2026-11-06 19:30:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7c2d9e4b318"
down_revision: Union[str, Sequence[str], None] = "e3a9f1c47d25"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("disputes") as batch_op:
        batch_op.add_column(
            sa.Column("seller_response_due_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("disputes") as batch_op:
        batch_op.drop_column("seller_response_due_at")
