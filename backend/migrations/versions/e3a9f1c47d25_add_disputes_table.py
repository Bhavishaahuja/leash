"""add disputes table

Revision ID: e3a9f1c47d25
Revises: b6f0c93e7a14
Create Date: 2026-11-06 14:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e3a9f1c47d25"
down_revision: Union[str, Sequence[str], None] = "b6f0c93e7a14"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "disputes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("paypal_dispute_id", sa.String(length=64), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=64), nullable=True),
        sa.Column("evidence_summary", sa.Text(), nullable=True),
        sa.Column("evidence_type", sa.String(length=64), nullable=True),
        sa.Column("evidence_claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evidence_submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["order_id"], ["orders.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("paypal_dispute_id"),
    )
    op.create_index(op.f("ix_disputes_order_id"), "disputes", ["order_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_disputes_order_id"), table_name="disputes")
    op.drop_table("disputes")
