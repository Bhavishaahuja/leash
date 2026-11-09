# The catalog: what the migration puts in, how the app reads it, and the sibling-variant pickers
# the demo scenarios use. Runs the real migration on a throwaway SQLite file like test_models.py.

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from backend.app import catalog
from backend.app.models import CatalogItem


@pytest.fixture
def migrated(tmp_path):
    url = "sqlite:///" + str(tmp_path / "test.db")
    alembic_config = Config("alembic.ini")
    alembic_config.set_main_option("sqlalchemy.url", url)
    command.upgrade(alembic_config, "head")
    return SimpleNamespace(engine=create_engine(url), config=alembic_config)


def rows_in(engine, category):
    with Session(engine) as session:
        return catalog.load_category(session, category)


# ---- what the migration seeds ----


def test_every_category_has_rows(migrated):
    for category in catalog.CATEGORIES:
        assert rows_in(migrated.engine, category), category


def test_sneakers_come_in_three_sizes_and_two_colors(migrated):
    rows = rows_in(migrated.engine, "sneakers")

    variants = set()
    for row in rows:
        variants.add((row.size, row.color))
    assert variants == {
        ("8", "white"), ("8.5", "white"), ("9", "white"),
        ("8", "cream"), ("8.5", "cream"), ("9", "cream"),
    }


def test_groceries_have_an_item_and_a_likely_substitute(migrated):
    rows = rows_in(migrated.engine, "groceries")

    names = []
    for row in rows:
        names.append(row.name)
        assert row.in_stock is True
    assert names == ["Organic oat milk", "Organic almond milk"]


def test_two_earbud_models_at_the_same_price(migrated):
    rows = rows_in(migrated.engine, "electronics")

    models = []
    prices = set()
    for row in rows:
        models.append(row.model)
        prices.add(row.price)
    assert models == ["FP-200", "FP-300"]
    assert prices == {Decimal("79.00")}


def test_tickets_are_one_event_on_two_dates(migrated):
    rows = rows_in(migrated.engine, "event tickets")

    names = set()
    dates = []
    for row in rows:
        names.add(row.name)
        dates.append(row.event_date)
        assert row.quantity_unit == "ticket"
    assert names == {"Fall Makers Market general admission"}
    assert dates == [date(2026, 12, 5), date(2026, 12, 6)]


def test_subscription_is_a_monthly_plan(migrated):
    rows = rows_in(migrated.engine, "subscription")

    assert len(rows) == 1
    assert rows[0].quantity_unit == "month"
    assert rows[0].price == Decimal("29.00")


def test_everything_is_at_the_fictional_store_with_a_unique_sku(migrated):
    with Session(migrated.engine) as session:
        rows = session.query(CatalogItem).all()

    skus = set()
    for row in rows:
        assert row.merchant == "Fernwood Supply Co"
        skus.add(row.sku)
    assert len(skus) == len(rows) == 13


def test_migration_goes_down_and_back_up(migrated):
    command.downgrade(migrated.config, "a7c2d9e4b318")
    assert "catalog_items" not in inspect(create_engine(str(migrated.engine.url))).get_table_names()

    command.upgrade(migrated.config, "head")
    assert len(rows_in(create_engine(str(migrated.engine.url)), "sneakers")) == 6


# ---- finding a mandate's rows ----


@pytest.mark.parametrize("category", ["Event Tickets", "  event   tickets ", "event tickets"])
def test_category_lookup_ignores_case_and_spacing(migrated, category):
    assert len(rows_in(migrated.engine, category)) == 2


@pytest.mark.parametrize("category", [None, "", "footwear", "outdoor gear"])
def test_category_the_store_doesnt_sell_has_no_rows(migrated, category):
    assert rows_in(migrated.engine, category) == []


# ---- the pickers, on plain rows ----


def row(**fields):
    defaults = {"name": "Thing", "size": None, "model": None, "event_date": None,
                "price": Decimal("10.00"), "in_stock": True}
    defaults.update(fields)
    return SimpleNamespace(**defaults)


SIZES = [row(size="8"), row(size="8.5"), row(size="9"), row(size="10")]


@pytest.mark.parametrize(
    ("wanted", "expected"),
    [("9", "8.5"), ("8", "8.5"), ("10", "9"), ("size 9", "size 8.5"), ("women's 8.5", "women's 8")],
)
def test_other_size_is_the_nearest_one_in_stock(wanted, expected):
    assert catalog.other_size(SIZES, wanted) == expected


@pytest.mark.parametrize("wanted", ["M", "large", "", None])
def test_other_size_needs_a_number_to_move(wanted):
    assert catalog.other_size(SIZES, wanted) is None


def test_other_size_with_only_the_one_size_is_none():
    assert catalog.other_size([row(size="9")], "9") is None


@pytest.mark.parametrize(
    ("catalog_name", "wanted", "same"),
    [
        ("Organic oat milk", "oat milk", True),
        ("Organic oat milk", "organic oat milk, 64 oz", True),
        ("Canvas low-top sneaker", "canvas sneakers", True),
        ("Canvas low-top sneaker", "sneakers", True),
        ("Organic almond milk", "organic oat milk", False),
        ("Pantry Box monthly plan", "wireless earbuds", False),
    ],
)
def test_same_product(catalog_name, wanted, same):
    assert catalog.same_product(catalog_name, wanted) is same


def test_other_model_skips_the_one_asked_for_whatever_the_case():
    rows = [row(model="FP-200"), row(model="FP-300")]

    assert catalog.other_model(rows, "fp-200") == "FP-300"


def test_other_model_falls_back_to_any_price_when_none_match():
    rows = [
        row(model="FP-200", price=Decimal("79.00")),
        row(model="FP-500", price=Decimal("199.00")),
    ]

    assert catalog.other_model(rows, "FP-200") == "FP-500"


def test_other_event_date_is_the_nearest_other_day():
    rows = [row(event_date=date(2026, 12, 5)), row(event_date=date(2026, 12, 20)),
            row(event_date=date(2026, 12, 6))]

    assert catalog.other_event_date(rows, date(2026, 12, 5)) == date(2026, 12, 6)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-12-05", date(2026, 12, 5)),
        ("2026-12-05T19:00:00+00:00", date(2026, 12, 5)),
        (date(2026, 12, 5), date(2026, 12, 5)),
        ("next Saturday", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_event_date(value, expected):
    assert catalog.parse_event_date(value) == expected
