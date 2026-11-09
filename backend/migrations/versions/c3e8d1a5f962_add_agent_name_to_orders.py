"""add agent_name to orders, backfill the seeded ones

Revision ID: c3e8d1a5f962
Revises: b91d6e2f4a73
Create Date: 2026-11-10 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3e8d1a5f962"
down_revision: Union[str, Sequence[str], None] = "b91d6e2f4a73"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# the two agents seed_demo.py splits its orders between, as named in .env.example. written out
# here, not read from settings: a migration can't depend on what's in one deploy's env vars
SEED_AGENT_ODD_IDS = "Wren"
SEED_AGENT_EVEN_IDS = "Thistle"
# SEED_LABEL_PREFIX in backend/app/demo.py, frozen
SEED_LABEL_PATTERN = "seed-demo:%"


def upgrade() -> None:
    with op.batch_alter_table("orders") as batch_op:
        batch_op.add_column(sa.Column("agent_name", sa.String(length=100), nullable=True))

    # only seeded orders. a real order's agent is unknown, and a made-up name would be a lie in
    # the dispute evidence, so those stay null. odd and even ids alternate like the seed does
    op.execute(
        sa.text(
            "UPDATE orders SET agent_name = CASE WHEN id % 2 = 1 THEN :odd ELSE :even END "
            "WHERE mandate_id IN (SELECT id FROM mandates WHERE buyer_label LIKE :pattern)"
        ).bindparams(odd=SEED_AGENT_ODD_IDS, even=SEED_AGENT_EVEN_IDS, pattern=SEED_LABEL_PATTERN)
    )


def downgrade() -> None:
    with op.batch_alter_table("orders") as batch_op:
        batch_op.drop_column("agent_name")
