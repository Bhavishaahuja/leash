"""add watch_mode to orders

Revision ID: b6f0c93e7a14
Revises: d5e8a2f41c07
Create Date: 2026-11-06 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b6f0c93e7a14"
down_revision: Union[str, Sequence[str], None] = "d5e8a2f41c07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # the server default fills every existing row with normal: the watch didn't exist when they
    # were checked, so nothing was ever decided in sharp mode
    with op.batch_alter_table("orders") as batch_op:
        batch_op.add_column(
            sa.Column(
                "watch_mode", sa.String(length=20), nullable=False, server_default="normal"
            )
        )
        batch_op.create_check_constraint("watch_mode", "watch_mode IN ('normal', 'sharp')")


def downgrade() -> None:
    with op.batch_alter_table("orders") as batch_op:
        batch_op.drop_constraint("watch_mode", type_="check")
        batch_op.drop_column("watch_mode")
