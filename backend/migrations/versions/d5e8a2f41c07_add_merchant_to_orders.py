"""add merchant to orders

Revision ID: d5e8a2f41c07
Revises: c81f3a6e92d4
Create Date: 2026-11-05 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d5e8a2f41c07"
down_revision: Union[str, Sequence[str], None] = "c81f3a6e92d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # nullable with no backfill: null means our own store, which is what every existing order is
    op.add_column("orders", sa.Column("merchant", sa.String(length=200), nullable=True))


def downgrade() -> None:
    op.drop_column("orders", "merchant")
