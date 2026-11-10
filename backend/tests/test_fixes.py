# The fix suggestion: plain Python over the catalog. The scenario tests build their wrong orders
# with the real demo builder and use the real catalog rows from the migration, so they're the same
# orders /place-order makes.

import importlib.util
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from keyed_client import MERCHANT_HEADERS
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import demo, fixes, orders, review
from backend.app.config import get_settings
from backend.app.db import Base, get_session
from backend.app.demo import DemoScenario, build_demo_order
from backend.app.models import CatalogItem, Mandate, Order
from backend.app.orders import item_to_json

MIGRATION_FILE = (
    Path(__file__).resolve().parents[1]
    / "migrations" / "versions" / "b91d6e2f4a73_add_catalog_items.py"
)
VAULT_ID = "test-fix-vault"

# one mandate per catalog category, the way the parser reads seed_demo.py's requests
MANDATES = {
    "sneakers": ("80.00", True, {"item": "canvas sneakers", "category": "sneakers",
                                 "color": "white", "size": "9", "quantity": 1}),
    "groceries": ("8.00", False, {"item": "organic oat milk", "category": "groceries",
                                  "size": "64 oz", "quantity": 1}),
    "electronics": ("90.00", True, {"item": "wireless earbuds", "category": "electronics",
                                    "model": "FP-200", "quantity": 1}),
    "event tickets": ("60.00", False, {"item": "Fall Makers Market general admission tickets",
                                       "category": "event tickets", "event_date": "2026-12-05",
                                       "quantity": 2}),
    "subscription": ("35.00", True, {"item": "Pantry Box monthly plan",
                                     "category": "subscription", "quantity": 1}),
}


def migration_catalog_rows():
    spec = importlib.util.spec_from_file_location("catalog_migration", MIGRATION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.catalog_rows()


@pytest.fixture
def sessions():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as session:
        for row in migration_catalog_rows():
            session.add(CatalogItem(**row))
        session.commit()
    return maker


@pytest.fixture
def session(sessions):
    with sessions() as session:
        yield session


def make_mandate(category, max_price=None, **rule_overrides):
    cap, inclusive, rules = MANDATES[category]
    rules = dict(rules)
    rules["currency"] = "USD"
    rules.update(rule_overrides)
    return Mandate(
        buyer_label="Sam", raw_request="x", rules=rules,
        max_price=Decimal(max_price or cap), max_price_inclusive=inclusive,
        merchant="Fernwood Supply Co",
    )


def order_for(category, scenario, **rule_overrides):
    # the order /place-order would place for this scenario
    mandate = make_mandate(category, **rule_overrides)
    rows = []
    for row in migration_catalog_rows():
        if row["category"] == category:
            rows.append(CatalogItem(**row))
    built = build_demo_order(mandate, scenario, rows)
    order = make_order(built.items, built.shipping, built.merchant)
    return mandate, order


def make_order(items, shipping=Decimal("0.00"), merchant=None):
    items_json = []
    for item in items:
        items_json.append(item_to_json(item))
    # the demo names our store, an agent order leaves it empty. both mean Fernwood
    if merchant == "Fernwood Supply Co":
        merchant = None
    return Order(id=7, items=items_json, total=Decimal("0"), shipping_amount=shipping,
                 currency="USD", merchant=merchant)


# ---- the five scenarios ----


def test_wrong_size_suggests_the_size_the_shopper_asked_for(session):
    mandate, order = order_for("sneakers", DemoScenario.wrong_size)

    fix = fixes.find_fix(session, mandate, order, ["size"])

    assert fix == {
        "sku": "FW-SNK-WHI-9",
        "item": "Canvas low-top sneaker",
        "price": "64.00",
        "quantity": 1,
        "sentence": "Agent picked size 8.5, mandate says 9. Size 9 is in stock at the same price.",
    }


def test_substitution_suggests_the_real_thing(session):
    mandate, order = order_for("groceries", DemoScenario.substitution)

    fix = fixes.find_fix(session, mandate, order, ["substitution"])

    assert fix["sku"] == "FW-GRO-OAT-64"
    assert fix["price"] == "4.99"
    assert fix["sentence"] == (
        "Agent picked Organic almond milk, mandate says organic oat milk. "
        "Organic oat milk is in stock at $4.99."
    )


def test_wrong_model_suggests_the_model_the_shopper_named(session):
    mandate, order = order_for("electronics", DemoScenario.wrong_model)

    fix = fixes.find_fix(session, mandate, order, ["model"])

    assert fix["sku"] == "FW-ELE-FP-200"
    # the catalog's price, not the 72.00 the demo order happened to pay
    assert fix["price"] == "79.00"
    assert fix["sentence"] == (
        "Agent picked model FP-300, mandate says FP-200. Model FP-200 is in stock at $79.00."
    )


def test_wrong_date_suggests_tickets_for_the_right_day(session):
    mandate, order = order_for("event tickets", DemoScenario.wrong_date)

    fix = fixes.find_fix(session, mandate, order, ["event_date"])

    assert fix["sku"] == "FW-TIX-MKT-1205"
    assert fix["quantity"] == 2
    assert fix["sentence"] == (
        "Agent picked Dec 6, 2026, mandate says Dec 5, 2026. Fall Makers Market general "
        "admission for Dec 5, 2026 is in stock at $25.00 each."
    )


def test_wrong_quantity_suggests_the_count_the_shopper_asked_for(session):
    mandate, order = order_for("subscription", DemoScenario.wrong_quantity)

    fix = fixes.find_fix(session, mandate, order, ["quantity"])

    assert fix["sku"] == "FW-SUB-PANTRY-1M"
    assert fix["quantity"] == 1
    assert fix["price"] == "29.00"
    assert fix["sentence"] == (
        "Agent picked 2 months, mandate says 1. Pantry Box monthly plan is in stock at $29.00."
    )


def test_a_quantity_fix_for_several_gives_the_total(session):
    # three tickets for a mandate that says two
    mandate, order = order_for("event tickets", DemoScenario.wrong_quantity)

    fix = fixes.find_fix(session, mandate, order, ["quantity"])

    assert fix["sentence"] == (
        "Agent picked 3 tickets, mandate says 2. Fall Makers Market general admission is in "
        "stock at $25.00 each, $50.00 for 2."
    )


def test_twelve_at_full_price_gets_a_quantity_fix_at_the_catalog_price(session):
    # the eval case: the price rule stops it and only names price, but the count is what's wrong.
    # the fix uses the catalog's $29.00, not whatever unit price the agent put on the order
    mandate = make_mandate("subscription")
    order = make_order([orders.OrderItemIn(name="Pantry Box monthly plan", quantity=12,
                                           unit_price="31.50")])

    fix = fixes.find_fix(session, mandate, order, ["price"])

    assert fix["quantity"] == 1
    assert fix["price"] == "29.00"
    assert fix["sentence"] == (
        "Agent picked 12 months, mandate says 1. Pantry Box monthly plan is in stock at $29.00."
    )


# ---- no fix available ----


@pytest.mark.parametrize(
    ("category", "scenario", "fields", "rule_overrides", "cap"),
    [
        # the store doesn't stock the size asked for
        ("sneakers", DemoScenario.clean, ["size"], {"size": "10"}, None),
        # or the color
        ("sneakers", DemoScenario.clean, ["color"], {"color": "black"}, None),
        # the right sneaker costs $64, over this shopper's $60
        ("sneakers", DemoScenario.wrong_size, ["size"], {}, "60.00"),
        # "under $50" for two $25 tickets is exactly $50, which isn't under
        ("event tickets", DemoScenario.wrong_date, ["event_date"], {}, "50.00"),
        # a model the store doesn't sell
        ("electronics", DemoScenario.wrong_model, ["model"], {"model": "FP-900"}, None),
    ],
)
def test_no_fix_when_nothing_in_stock_fits_the_whole_mandate(
    session, category, scenario, fields, rule_overrides, cap
):
    mandate, order = order_for(category, scenario, **rule_overrides)
    if cap is not None:
        mandate.max_price = Decimal(cap)

    assert fixes.find_fix(session, mandate, order, fields) is None


def test_no_fix_when_the_right_item_is_out_of_stock(session):
    for row in session.query(CatalogItem).filter_by(sku="FW-GRO-OAT-64"):
        row.in_stock = False
    session.commit()
    mandate, order = order_for("groceries", DemoScenario.substitution)

    assert fixes.find_fix(session, mandate, order, ["substitution"]) is None


def test_no_fix_from_another_store(session):
    # the order is at Ridgeline. our catalog says nothing about what Ridgeline has in stock
    mandate, order = order_for("sneakers", DemoScenario.wrong_size)
    order.merchant = "Ridgeline Outdoors"

    assert fixes.find_fix(session, mandate, order, ["size"]) is None


@pytest.mark.parametrize(
    "fields",
    [[], ["merchant"], ["expiry"], ["instructions_in_order"], ["warranty"], None],
)
def test_no_fix_for_problems_a_catalog_item_cant_solve(session, fields):
    mandate, order = order_for("sneakers", DemoScenario.wrong_size)

    assert fixes.find_fix(session, mandate, order, fields) is None


def test_no_fix_for_a_mandate_outside_the_catalog(session):
    mandate = Mandate(buyer_label="Sam", raw_request="x", max_price=Decimal("120.00"),
                      rules={"item": "trail runners", "category": "footwear", "size": "9"})
    order = make_order([orders.OrderItemIn(name="Trail runner", size="8", quantity=1,
                                           unit_price="98.00")])

    assert fixes.find_fix(session, mandate, order, ["size"]) is None


def test_no_fix_for_a_cap_in_another_currency(session):
    mandate, order = order_for("sneakers", DemoScenario.wrong_size)
    mandate.rules["currency"] = "EUR"

    assert fixes.find_fix(session, mandate, order, ["size"]) is None


# ---- picking between several that fit ----


def test_the_closest_price_to_the_original_wins(session):
    # two more white size 9s at other prices. the agent paid 64.00, so the 64.00 one stays
    for sku, price in [("FW-SNK-WHT-9-SALE", "49.00"), ("FW-SNK-WHT-9-LTD", "66.00")]:
        session.add(CatalogItem(sku=sku, name="Canvas low-top sneaker", category="sneakers",
                                size="9", color="white", quantity_unit="pair",
                                price=Decimal(price), in_stock=True, merchant="Fernwood Supply Co"))
    session.commit()
    mandate, order = order_for("sneakers", DemoScenario.wrong_size)
    order.items[0]["unit_price"] = "65.50"

    fix = fixes.find_fix(session, mandate, order, ["size"])

    # 66.00 is 0.50 away, 64.00 is 1.50 away
    assert fix["sku"] == "FW-SNK-WHT-9-LTD"
    assert fix["sentence"].endswith("Size 9 is in stock at $66.00.")


def test_size_matches_however_the_shopper_wrote_it(session):
    mandate, order = order_for("sneakers", DemoScenario.wrong_size, size="men's size 9")

    fix = fixes.find_fix(session, mandate, order, ["size"])

    assert fix["sku"] == "FW-SNK-WHI-9"
    # no "size men's size 8.5": the shopper's own wording already says size
    assert fix["sentence"] == (
        "Agent picked men's size 8.5, mandate says men's size 9. "
        "Size 9 is in stock at the same price."
    )


def test_size_without_the_word_size_gets_it_added(session):
    mandate, order = order_for("sneakers", DemoScenario.wrong_size, size="men's 9")

    fix = fixes.find_fix(session, mandate, order, ["size"])

    assert fix["sentence"].startswith("Agent picked size men's 8.5, mandate says men's 9.")


# ---- wired into the order flow ----


class FieldClaude:
    # flags whatever the test set, like the real one would for that scenario
    def __init__(self):
        self.fields = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def with_options(self, **options):
        return self

    def _create(self, **kwargs):
        if self.fields:
            answer = {"verdict": "mismatch", "risk_score": 90, "reason": "Wrong.",
                      "mismatched_fields": self.fields}
        else:
            answer = {"verdict": "match", "risk_score": 5, "reason": "Matches.",
                      "mismatched_fields": []}
        block = SimpleNamespace(type="tool_use", name=kwargs["tools"][0]["name"], input=answer)
        return SimpleNamespace(stop_reason="tool_use", content=[block])


class FakePayPal:
    def create_order(self, body, request_id=None):
        return {"id": "PP" + uuid.uuid4().hex[:10], "status": "COMPLETED", "purchase_units": [{
            "payments": {"authorizations": [{
                "id": "AUTH" + uuid.uuid4().hex[:10],
                "status": "CREATED",
                "expiration_time": "2099-01-01T00:00:00Z",
            }]}
        }]}

    def capture_authorization(self, authorization_id, request_id=None, budget_seconds=None):
        return {"id": "CAP" + authorization_id, "status": "COMPLETED"}


@pytest.fixture
def claude():
    return FieldClaude()


@pytest.fixture
def client(sessions, claude, monkeypatch):
    monkeypatch.setenv("DEMO_VAULT_ID", VAULT_ID)
    get_settings.cache_clear()

    app = FastAPI()
    app.include_router(orders.router)
    app.include_router(demo.router)
    app.include_router(review.router)

    def test_session():
        session = sessions()
        try:
            yield session
        finally:
            session.close()

    paypal = FakePayPal()
    app.dependency_overrides[get_session] = test_session
    app.dependency_overrides[orders.get_anthropic_client] = lambda: claude
    app.dependency_overrides[orders.get_paypal_client] = lambda: paypal
    return TestClient(app)


@pytest.fixture
def sneaker_mandate_id(sessions):
    with sessions() as session:
        mandate = make_mandate("sneakers")
        session.add(mandate)
        session.commit()
        return mandate.id


def place(client, mandate_id, scenario):
    return client.post("/demo/orders", json={"mandate_id": mandate_id, "scenario": scenario},
                       headers=MERCHANT_HEADERS)


def test_a_held_order_gets_its_fix_in_the_order_api(client, claude, sneaker_mandate_id):
    claude.fields = ["size"]

    placed = place(client, sneaker_mandate_id, "wrong_size").json()
    detail = client.get("/orders/" + str(placed["id"]), headers=MERCHANT_HEADERS).json()
    listed = client.get("/orders", headers=MERCHANT_HEADERS).json()[0]

    assert placed["review_state"] == "needs_review"
    for check in [placed["intent_check"], detail["intent_check"], listed["intent_check"],
                  detail["decisions"][-1]]:
        assert check["suggested_fix"]["sku"] == "FW-SNK-WHI-9"
        assert check["suggested_fix"]["price"] == "64.00"


def test_the_review_page_gets_the_fix(client, claude, sneaker_mandate_id):
    claude.fields = ["size"]
    order_id = place(client, sneaker_mandate_id, "wrong_size").json()["id"]
    token = client.post(
        "/orders/" + str(order_id) + "/review-link", headers=MERCHANT_HEADERS
    ).json()["review_token"]

    view = client.get("/review/" + token).json()

    assert view["intent_check"]["suggested_fix"]["sentence"] == (
        "Agent picked size 8.5, mandate says 9. Size 9 is in stock at the same price."
    )


def test_an_auto_captured_order_gets_no_fix(client, claude, sneaker_mandate_id):
    claude.fields = []

    placed = place(client, sneaker_mandate_id, "clean").json()

    assert placed["status"] == "captured"
    assert placed["intent_check"]["suggested_fix"] is None


def test_a_hard_rule_failure_with_nothing_to_fix_gets_no_fix(client, sneaker_mandate_id):
    # over the cap with the right item and quantity: there's no better item to suggest
    placed = place(client, sneaker_mandate_id, "over_cap").json()

    assert placed["review_state"] == "needs_review"
    assert placed["intent_check"]["suggested_fix"] is None


def test_a_broken_fix_lookup_cant_fail_the_order(client, claude, sneaker_mandate_id, monkeypatch):
    # the agent is waiting on this response. a crash in a nice-to-have mustn't look like the
    # purchase failed and make it buy again
    def broken(*args):
        raise RuntimeError("catalog is on fire")

    monkeypatch.setattr(fixes, "find_fix", broken)
    claude.fields = ["size"]

    response = place(client, sneaker_mandate_id, "wrong_size")

    assert response.status_code == 201
    assert response.json()["review_state"] == "needs_review"
    assert response.json()["intent_check"]["suggested_fix"] is None
