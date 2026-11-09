"""add catalog_items, seeded with the demo store's products

Revision ID: b91d6e2f4a73
Revises: a7c2d9e4b318
Create Date: 2026-11-09 12:00:00.000000

"""
from datetime import date
from decimal import Decimal
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b91d6e2f4a73"
down_revision: Union[str, Sequence[str], None] = "a7c2d9e4b318"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

STORE = "Fernwood Supply Co"


def catalog_rows():
    # written out here on purpose, not imported from the app. a migration has to keep doing what
    # it did the day it ran, whatever catalog.py looks like later
    rows = []

    # every size in both colors, so a wrong size can keep the color and a wrong color the size
    for color in ["white", "cream"]:
        for size in ["8", "8.5", "9"]:
            rows.append({
                "sku": "FW-SNK-" + color[:3].upper() + "-" + size.replace(".", "H"),
                "name": "Canvas low-top sneaker",
                "category": "sneakers",
                "size": size,
                "color": color,
                "model": None,
                "event_date": None,
                "quantity_unit": "pair",
                "price": Decimal("64.00"),
            })

    # the thing people ask for and the thing an agent swaps in when it's gone
    rows.append({
        "sku": "FW-GRO-OAT-64", "name": "Organic oat milk", "category": "groceries",
        "size": "64 oz", "color": None, "model": None, "event_date": None,
        "quantity_unit": "carton", "price": Decimal("4.99"),
    })
    rows.append({
        "sku": "FW-GRO-ALM-64", "name": "Organic almond milk", "category": "groceries",
        "size": "64 oz", "color": None, "model": None, "event_date": None,
        "quantity_unit": "carton", "price": Decimal("4.79"),
    })

    # same price on purpose, so the total can't give the wrong one away
    for model in ["FP-200", "FP-300"]:
        rows.append({
            "sku": "FW-ELE-" + model, "name": "Wireless earbuds", "category": "electronics",
            "size": None, "color": None, "model": model, "event_date": None,
            "quantity_unit": "pair", "price": Decimal("79.00"),
        })

    # one event, two days
    for event_date in [date(2026, 12, 5), date(2026, 12, 6)]:
        rows.append({
            "sku": "FW-TIX-MKT-" + event_date.strftime("%m%d"),
            "name": "Fall Makers Market general admission", "category": "event tickets",
            "size": None, "color": None, "model": None, "event_date": event_date,
            "quantity_unit": "ticket", "price": Decimal("25.00"),
        })

    rows.append({
        "sku": "FW-SUB-PANTRY-1M", "name": "Pantry Box monthly plan", "category": "subscription",
        "size": None, "color": None, "model": None, "event_date": None,
        "quantity_unit": "month", "price": Decimal("29.00"),
    })

    for row in rows:
        row["in_stock"] = True
        row["merchant"] = STORE
    return rows


def upgrade() -> None:
    catalog_items = op.create_table(
        "catalog_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("size", sa.String(length=50), nullable=True),
        sa.Column("color", sa.String(length=50), nullable=True),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("event_date", sa.Date(), nullable=True),
        sa.Column("quantity_unit", sa.String(length=20), nullable=False),
        sa.Column("price", sa.Numeric(precision=10, scale=2), nullable=False),
        sa.Column("in_stock", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("merchant", sa.String(length=200), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sku"),
    )
    op.create_index(op.f("ix_catalog_items_category"), "catalog_items", ["category"])
    op.bulk_insert(catalog_items, catalog_rows())


def downgrade() -> None:
    op.drop_index(op.f("ix_catalog_items_category"), table_name="catalog_items")
    op.drop_table("catalog_items")
