"""link replaced orders, add accept_fix as an approval action

Revision ID: e5b1c8f3a207
Revises: d7f2a9c4e183
Create Date: 2026-11-10 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5b1c8f3a207"
down_revision: Union[str, Sequence[str], None] = "d7f2a9c4e183"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def replace_action_constraint(allowed_values):
    # the enum's CHECK carries the enum's name, see the initial migration. batch mode because
    # SQLite can't alter a constraint in place, on Postgres it's a plain ALTER TABLE
    with op.batch_alter_table("approvals") as batch_op:
        batch_op.drop_constraint("approval_action", type_="check")
        batch_op.create_check_constraint("approval_action", "action IN " + allowed_values)


def upgrade() -> None:
    # null on every existing order, none of them were replaced
    with op.batch_alter_table("orders") as batch_op:
        batch_op.add_column(sa.Column("replaced_by_order_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("replaces_order_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_orders_replaced_by_order_id", "orders", ["replaced_by_order_id"], ["id"]
        )
        batch_op.create_foreign_key(
            "fk_orders_replaces_order_id", "orders", ["replaces_order_id"], ["id"]
        )

    replace_action_constraint("('approve', 'reject', 'accept_fix')")


def downgrade() -> None:
    # an accepted fix voided the order, which is what a reject did before this existed
    op.execute("UPDATE approvals SET action = 'reject' WHERE action = 'accept_fix'")
    replace_action_constraint("('approve', 'reject')")

    with op.batch_alter_table("orders") as batch_op:
        batch_op.drop_constraint("fk_orders_replaces_order_id", type_="foreignkey")
        batch_op.drop_constraint("fk_orders_replaced_by_order_id", type_="foreignkey")
        batch_op.drop_column("replaces_order_id")
        batch_op.drop_column("replaced_by_order_id")
