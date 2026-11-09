# POST /demo/orders and GET /mandates. The builder needs no fakes at all. The route tests go through
# the real place_order with PayPal and Claude faked, so nothing here reaches the network.

import html
import importlib.util
import json
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from keyed_client import AGENT_HEADERS, MERCHANT_HEADERS
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import demo, mandates, orders
from backend.app.config import get_settings
from backend.app.db import Base, get_session
from backend.app.demo import DemoScenario, ScenarioNotPossible, build_demo_order
from backend.app.intent.rules import check_merchant
from backend.app.models import CatalogItem, Mandate, Order
from backend.app.orders import OrderItemIn, item_display_name

VAULT_ID = "test-demo-vault"

MIGRATION_FILE = (
    Path(__file__).resolve().parents[1]
    / "migrations" / "versions" / "b91d6e2f4a73_add_catalog_items.py"
)


def migration_catalog_rows():
    # the rows the real migration inserts, so these tests can't drift from the store's catalog
    spec = importlib.util.spec_from_file_location("catalog_migration", MIGRATION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.catalog_rows()


def catalog_for(category):
    rows = []
    for row in migration_catalog_rows():
        if row["category"] == category:
            rows.append(CatalogItem(**row))
    return rows


def make_mandate(max_price="120.00", merchant="Fernwood Supply Co", **rule_overrides):
    rules = {
        "item": "Trail runners",
        "color": "black",
        "size": "women's 9",
        "quantity": 1,
        "currency": "USD",
    }
    rules.update(rule_overrides)
    cap = Decimal(max_price) if max_price is not None else None
    return Mandate(
        buyer_label="Sam",
        raw_request="Black trail runners, women's size 9, up to $120, from Fernwood Supply Co.",
        rules=rules,
        max_price=cap,
        max_price_inclusive=True,
        merchant=merchant,
    )


# one mandate per catalog category, the way the parser turns seed_demo.py's requests into rules
CATALOG_MANDATES = {
    "sneakers": {
        "request": "White canvas sneakers in a size 9, nothing over $80, from Fernwood Supply Co.",
        "max_price": "80.00",
        "rules": {"item": "canvas sneakers", "category": "sneakers", "color": "white",
                  "size": "9", "quantity": 1},
    },
    "groceries": {
        "request": "Grab a 64 oz carton of organic oat milk from Fernwood Supply Co, under $8.",
        "max_price": "8.00",
        "rules": {"item": "organic oat milk", "category": "groceries", "size": "64 oz",
                  "quantity": 1},
    },
    "electronics": {
        "request": "Get me the FP-200 wireless earbuds from Fernwood Supply Co, up to $90.",
        "max_price": "90.00",
        "rules": {"item": "wireless earbuds", "category": "electronics", "model": "FP-200",
                  "quantity": 1},
    },
    "event tickets": {
        "request": "Two general admission tickets to the Fall Makers Market on Saturday, "
        "December 5, 2026, under $60 total, from Fernwood Supply Co.",
        "max_price": "60.00",
        "rules": {"item": "Fall Makers Market general admission tickets",
                  "category": "event tickets", "event_date": "2026-12-05", "quantity": 2},
    },
    "subscription": {
        "request": "Sign me up for one month of the Pantry Box monthly plan from Fernwood "
        "Supply Co, max $35.",
        "max_price": "35.00",
        "rules": {"item": "Pantry Box monthly plan", "category": "subscription", "quantity": 1},
    },
}


def catalog_mandate(category, **rule_overrides):
    spec = CATALOG_MANDATES[category]
    rules = {"color": None, "size": None, "model": None, "event_date": None, "currency": "USD"}
    rules.update(spec["rules"])
    rules.update(rule_overrides)
    return Mandate(
        buyer_label="Sam",
        raw_request=spec["request"],
        rules=rules,
        max_price=Decimal(spec["max_price"]),
        max_price_inclusive=True,
        merchant="Fernwood Supply Co",
    )


def build_for(category, scenario, **rule_overrides):
    mandate = catalog_mandate(category, **rule_overrides)
    return build_demo_order(mandate, scenario, catalog_for(category))


def changed_fields(clean, other):
    # every field that differs between two demo orders, merchant included
    clean_fields = clean.items[0].model_dump()
    clean_fields["merchant"] = clean.merchant
    other_fields = other.items[0].model_dump()
    other_fields["merchant"] = other.merchant

    differences = set()
    for field in clean_fields:
        if clean_fields[field] != other_fields[field]:
            differences.add(field)
    return differences


def total_of(demo_order):
    total = demo_order.shipping
    for item in demo_order.items:
        total += item.unit_price * item.quantity
    return total


# ---- the builder, one scenario at a time ----


def test_clean_matches_the_mandate_at_about_80_percent_of_the_cap():
    demo_order = build_demo_order(make_mandate(), DemoScenario.clean)

    item = demo_order.items[0]
    assert item.name == "Trail runners"
    assert item.color == "black"
    assert item.size == "women's 9"
    assert item.quantity == 1
    assert total_of(demo_order) == Decimal("96.00")
    assert demo_order.merchant == "Fernwood Supply Co"


def test_wrong_color_only_changes_the_color():
    clean = build_demo_order(make_mandate(), DemoScenario.clean)

    demo_order = build_demo_order(make_mandate(), DemoScenario.wrong_color)

    assert demo_order.items[0].color == "red"
    assert demo_order.items[0].name == clean.items[0].name
    assert demo_order.items[0].size == clean.items[0].size
    assert total_of(demo_order) == total_of(clean)
    assert demo_order.merchant == clean.merchant


def test_wrong_color_never_picks_the_color_the_mandate_asked_for():
    demo_order = build_demo_order(make_mandate(color="Dark Red"), DemoScenario.wrong_color)

    assert demo_order.items[0].color == "green"


def test_over_cap_is_about_15_percent_over_and_otherwise_matches():
    demo_order = build_demo_order(make_mandate(), DemoScenario.over_cap)

    assert total_of(demo_order) == Decimal("138.00")
    assert demo_order.items[0].color == "black"
    assert demo_order.merchant == "Fernwood Supply Co"


def test_wrong_store_uses_the_other_seed_store_and_otherwise_matches():
    demo_order = build_demo_order(make_mandate(), DemoScenario.wrong_store)

    assert demo_order.merchant == "Ridgeline Outdoors"
    assert demo_order.items[0].color == "black"
    assert total_of(demo_order) == Decimal("96.00")


def test_wrong_store_on_a_ridgeline_mandate_is_still_a_different_store():
    mandate = make_mandate(merchant="ridgeline outdoors.")

    demo_order = build_demo_order(mandate, DemoScenario.wrong_store)

    assert demo_order.merchant == "Fernwood Supply Co"


# ---- the builder, odd mandates ----


@pytest.mark.parametrize(
    ("max_price", "quantity", "scenario", "at_least", "at_most"),
    [
        # thirds don't divide evenly, so rounding has to stay on the right side of the line
        ("100.00", 3, DemoScenario.clean, "79.00", "80.00"),
        ("100.00", 3, DemoScenario.over_cap, "115.00", "116.00"),
        ("0.10", 3, DemoScenario.over_cap, "0.11", "0.15"),
    ],
)
def test_the_cap_covers_the_whole_quantity(max_price, quantity, scenario, at_least, at_most):
    demo_order = build_demo_order(make_mandate(max_price, quantity=quantity), scenario)

    assert demo_order.items[0].quantity == quantity
    assert Decimal(at_least) <= total_of(demo_order) <= Decimal(at_most)


def test_null_quantity_means_one():
    demo_order = build_demo_order(make_mandate(quantity=None), DemoScenario.clean)

    assert demo_order.items[0].quantity == 1


def test_no_cap_still_builds_a_clean_order():
    demo_order = build_demo_order(make_mandate(max_price=None), DemoScenario.clean)

    assert total_of(demo_order) == Decimal("50.00")


def test_clean_item_name_comes_from_the_mandate_and_reads_like_a_product_line():
    mandate = make_mandate(item="trail runners", color="blue", size="8")

    item = build_demo_order(mandate, DemoScenario.clean).items[0]

    assert "trail runners" in item.name.lower()
    # what PayPal gets. the page joins the same three fields the same way
    assert item_display_name(item) == "Trail runners, blue, size 8"


def test_a_mandate_with_only_an_item_has_a_plain_product_line():
    mandate = make_mandate(item="camping mug", color=None, size=None)

    item = build_demo_order(mandate, DemoScenario.clean).items[0]

    assert item_display_name(item) == "Camping mug"


def test_a_size_that_already_says_size_isnt_doubled():
    # the live demo mandate stores "size women's size 9", which used to come out "size size ..."
    mandate = make_mandate(item="trail runners", color="black", size="size women's 9")

    item = build_demo_order(mandate, DemoScenario.clean).items[0]

    assert item_display_name(item) == "Trail runners, black, size women's 9"


def test_wrong_color_differs_from_clean_only_in_color():
    clean = build_demo_order(make_mandate(), DemoScenario.clean).items[0].model_dump()

    wrong = build_demo_order(make_mandate(), DemoScenario.wrong_color).items[0].model_dump()

    differences = []
    for field in clean:
        if clean[field] != wrong[field]:
            differences.append(field)
    assert differences == ["color"]


@pytest.mark.parametrize(
    ("scenario", "changed"),
    [
        (DemoScenario.wrong_color, {"color"}),
        (DemoScenario.over_cap, {"unit_price"}),
        (DemoScenario.wrong_store, {"merchant"}),
    ],
)
def test_each_scenario_breaks_exactly_one_thing(scenario, changed):
    clean = build_demo_order(make_mandate(), DemoScenario.clean)
    other = build_demo_order(make_mandate(), scenario)

    clean_fields = clean.items[0].model_dump()
    clean_fields["merchant"] = clean.merchant
    other_fields = other.items[0].model_dump()
    other_fields["merchant"] = other.merchant

    differences = set()
    for field in clean_fields:
        if clean_fields[field] != other_fields[field]:
            differences.add(field)
    assert differences == changed


@pytest.mark.parametrize("item", [None, "", "   "])
@pytest.mark.parametrize("scenario", list(DemoScenario))
def test_a_mandate_with_no_item_refuses_every_scenario(item, scenario):
    # a category isn't enough either, "footwear" isn't something Claude can match an order against
    mandate = make_mandate(item=item, category="footwear")

    with pytest.raises(ScenarioNotPossible):
        build_demo_order(mandate, scenario)


@pytest.mark.parametrize(
    ("mandate_args", "scenario"),
    [
        ({"color": None}, DemoScenario.wrong_color),
        ({"max_price": None}, DemoScenario.over_cap),
        ({"merchant": None}, DemoScenario.wrong_store),
        # a cent split between two rounds down to nothing, which isn't a valid price
        ({"max_price": "0.01", "quantity": 2}, DemoScenario.clean),
        ({"quantity": 1001}, DemoScenario.clean),
    ],
)
def test_scenario_with_nothing_to_break_refuses(mandate_args, scenario):
    with pytest.raises(ScenarioNotPossible):
        build_demo_order(make_mandate(**mandate_args), scenario)


# ---- the merchant rule ----


def test_merchant_rule_catches_an_order_at_another_store():
    result = check_merchant(make_mandate(), "Ridgeline Outdoors")

    assert result.passed is False
    assert result.reason == "Mandate says Fernwood Supply Co, order is at Ridgeline Outdoors"


def test_merchant_rule_with_no_order_merchant_means_our_store():
    assert check_merchant(make_mandate(merchant="fernwood")).passed is True


def test_merchant_rule_doesnt_lend_our_aliases_to_another_store():
    # "Fernwood" is a nickname for our store, it doesn't match an order at Ridgeline
    assert check_merchant(make_mandate(merchant="fernwood"), "Ridgeline Outdoors").passed is False


# ---- the routes ----


class FakeClaude:
    # the one call it can get here is the order check. it compares the parsed mandate fields with
    # the order's fields one by one, a much blunter version of what the real one reads
    def __init__(self):
        self.calls = 0
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def with_options(self, **options):
        return self

    def _create(self, **kwargs):
        self.calls += 1
        content = kwargs["messages"][0]["content"]
        wanted = tag_json(content, "parsed_fields")
        ordered = tag_json(content, "order")["items"][0]

        differences = []
        wanted_item = (wanted.get("item") or "").casefold()
        ordered_item = ordered["name"].casefold()
        if wanted_item not in ordered_item and ordered_item not in wanted_item:
            differences.append("substitution")
        for field in ["color", "size", "model", "event_date", "quantity"]:
            if wanted.get(field) is not None and str(wanted[field]) != str(ordered.get(field)):
                differences.append(field)

        if differences:
            fields = {"verdict": "mismatch", "risk_score": 90,
                      "reason": "Wrong " + differences[0] + ".", "mismatched_fields": differences}
        else:
            fields = {"verdict": "match", "risk_score": 5, "reason": "Matches.",
                      "mismatched_fields": []}
        block = SimpleNamespace(type="tool_use", name=kwargs["tools"][0]["name"], input=fields)
        return SimpleNamespace(stop_reason="tool_use", content=[block])


def tag_json(content, tag):
    inside = content.split("<" + tag + ">")[1].split("</" + tag + ">")[0]
    return json.loads(html.unescape(inside))


class FakePayPal:
    def __init__(self):
        self.bodies = []
        self.captured = []

    def create_order(self, body, request_id=None):
        self.bodies.append(body)
        return {"id": "PP" + uuid.uuid4().hex[:10], "status": "COMPLETED", "purchase_units": [{
            "payments": {"authorizations": [{
                "id": "AUTH" + uuid.uuid4().hex[:10],
                "status": "CREATED",
                "expiration_time": "2099-01-01T00:00:00Z",
            }]}
        }]}

    def capture_authorization(self, authorization_id, request_id=None, budget_seconds=None):
        self.captured.append(authorization_id)
        return {"id": "CAP" + authorization_id, "status": "COMPLETED"}


@pytest.fixture
def sessions():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def claude():
    return FakeClaude()


@pytest.fixture
def paypal():
    return FakePayPal()


@pytest.fixture
def client(sessions, claude, paypal, monkeypatch):
    monkeypatch.setenv("DEMO_VAULT_ID", VAULT_ID)
    get_settings.cache_clear()

    app = FastAPI()
    app.include_router(mandates.router)
    app.include_router(orders.router)
    app.include_router(demo.router)

    def test_session():
        session = sessions()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = test_session
    app.dependency_overrides[orders.get_anthropic_client] = lambda: claude
    app.dependency_overrides[orders.get_paypal_client] = lambda: paypal
    return TestClient(app)


@pytest.fixture
def mandate_id(sessions):
    with sessions() as session:
        mandate = make_mandate()
        session.add(mandate)
        session.commit()
        return mandate.id


def place(client, mandate_id, scenario):
    return client.post(
        "/demo/orders",
        json={"mandate_id": mandate_id, "scenario": scenario},
        headers=MERCHANT_HEADERS,
    )


def test_clean_order_auto_captures_and_returns_the_full_order(client, mandate_id, paypal):
    response = place(client, mandate_id, "clean")

    assert response.status_code == 201
    order = response.json()
    assert order["status"] == "captured"
    assert order["review_state"] == "auto_approved"
    assert order["intent_check"]["verdict"] == "match"
    assert order["intent_check"]["risk_score"] == 5
    assert order["intent_check"]["reason"] == "Matches."
    assert order["vault_id"] == VAULT_ID
    assert order["mandate"]["id"] == mandate_id
    assert len(paypal.captured) == 1


@pytest.mark.parametrize(
    ("scenario", "field", "checked_by", "reason"),
    [
        ("wrong_color", "color", "ai", "Wrong color."),
        ("over_cap", "price", "rules", "Order is $138.00, mandate cap is $120.00"),
        (
            "wrong_store",
            "merchant",
            "rules",
            "Mandate says Fernwood Supply Co, order is at Ridgeline Outdoors",
        ),
    ],
)
def test_bad_scenarios_hold_for_review_and_never_capture(
    client, mandate_id, paypal, scenario, field, checked_by, reason
):
    response = place(client, mandate_id, scenario)

    assert response.status_code == 201
    order = response.json()
    assert order["status"] == "authorized"
    assert order["review_state"] == "needs_review"
    assert order["intent_check"]["mismatched_fields"] == [field]
    assert order["intent_check"]["checked_by"] == checked_by
    assert order["intent_check"]["reason"] == reason
    assert paypal.captured == []


def test_wrong_store_is_stopped_by_the_rule_before_claude(client, mandate_id, claude):
    # without the merchant on the order this would reach Claude, which is told not to judge the
    # store, and it would auto-capture
    order = place(client, mandate_id, "wrong_store").json()

    assert claude.calls == 0
    assert order["merchant"] == "Ridgeline Outdoors"


def test_demo_order_goes_through_place_order(client, mandate_id, monkeypatch):
    # the whole point: one path. if the route grew its own copy this would never get called
    calls = []
    real_place_order = demo.place_order

    def spy(*args, **kwargs):
        calls.append(kwargs["merchant"])
        return real_place_order(*args, **kwargs)

    monkeypatch.setattr(demo, "place_order", spy)

    assert place(client, mandate_id, "clean").status_code == 201
    assert calls == ["Fernwood Supply Co"]


def test_an_agents_order_still_has_no_merchant(client, mandate_id, sessions):
    # the agent's request has no way to name a store, so its orders stay at ours
    response = client.post(
        "/orders",
        json={
            "mandate_id": mandate_id,
            "vault_id": VAULT_ID,
            "merchant": "Ridgeline Outdoors",
            "items": [{"name": "Trail runners", "color": "black", "size": "women's 9",
                       "quantity": 1, "unit_price": "96.00"}],
        },
        headers=AGENT_HEADERS,
    )

    assert response.status_code == 201
    assert response.json()["review_state"] == "auto_approved"
    with sessions() as session:
        assert session.query(Order).one().merchant is None


def test_scenario_that_cant_work_is_a_422_and_places_nothing(client, sessions, paypal):
    with sessions() as session:
        mandate = make_mandate(color=None)
        session.add(mandate)
        session.commit()
        mandate_id = mandate.id

    response = place(client, mandate_id, "wrong_color")

    assert response.status_code == 422
    assert response.json()["detail"] == "This mandate doesn't name a color to get wrong"
    assert paypal.bodies == []


@pytest.mark.parametrize("scenario", ["clean", "wrong_color", "over_cap", "wrong_store"])
def test_mandate_with_no_item_is_a_422_and_places_nothing(client, sessions, paypal, scenario):
    # what a row with empty rules gets, like one that never went through the parser
    with sessions() as session:
        mandate = Mandate(buyer_label="Sam", raw_request="Trail runners", rules={})
        session.add(mandate)
        session.commit()
        mandate_id = mandate.id

    response = place(client, mandate_id, scenario)

    assert response.status_code == 422
    assert response.json()["detail"] == "This mandate doesn't name an item to order"
    assert paypal.bodies == []


def test_unknown_scenario_is_a_422(client, mandate_id, paypal):
    assert place(client, mandate_id, "free_money").status_code == 422
    assert paypal.bodies == []


def test_unknown_mandate_is_a_404(client):
    assert place(client, 999, "clean").status_code == 404


def test_missing_vault_id_is_a_503_and_places_nothing(client, mandate_id, paypal, monkeypatch):
    monkeypatch.setenv("DEMO_VAULT_ID", "")
    get_settings.cache_clear()

    response = place(client, mandate_id, "clean")

    assert response.status_code == 503
    assert paypal.bodies == []


@pytest.mark.parametrize("headers", [{}, AGENT_HEADERS, {"X-API-Key": "wrong"}])
def test_placing_a_demo_order_needs_the_merchant_side(client, mandate_id, paypal, headers):
    response = client.post(
        "/demo/orders", json={"mandate_id": mandate_id, "scenario": "clean"}, headers=headers
    )

    assert response.status_code == 401
    assert paypal.bodies == []


# ---- GET /mandates ----


def test_list_mandates_newest_first_with_the_parsed_rules(client, sessions):
    with sessions() as session:
        session.add(make_mandate())
        session.add(make_mandate(max_price="40.00", color="navy"))
        session.commit()

    response = client.get("/mandates", headers=MERCHANT_HEADERS)

    assert response.status_code == 200
    newest, oldest = response.json()
    assert newest["id"] > oldest["id"]
    assert newest["raw_request"].startswith("Black trail runners")
    assert newest["rules"]["item"] == "Trail runners"
    assert newest["rules"]["color"] == "navy"
    assert newest["rules"]["size"] == "women's 9"
    assert newest["max_price"] == "40.00"
    assert newest["merchant"] == "Fernwood Supply Co"
    assert newest["expires_at"] is None


def test_list_mandates_respects_the_limit(client, sessions):
    with sessions() as session:
        session.add(make_mandate())
        session.add(make_mandate())
        session.commit()

    assert len(client.get("/mandates?limit=1", headers=MERCHANT_HEADERS).json()) == 1


@pytest.mark.parametrize("headers", [{}, AGENT_HEADERS, {"X-API-Key": "wrong"}])
def test_list_mandates_needs_the_merchant_side(client, headers):
    assert client.get("/mandates", headers=headers).status_code == 401


# ---- the catalog scenarios: the builder ----


def test_clean_catalog_orders_carry_model_and_event_date():
    earbuds = build_for("electronics", DemoScenario.clean).items[0]
    tickets = build_for("event tickets", DemoScenario.clean).items[0]

    assert earbuds.model == "FP-200"
    assert earbuds.event_date is None
    assert tickets.event_date == date(2026, 12, 5)
    assert tickets.quantity == 2


@pytest.mark.parametrize(
    ("category", "scenario", "changed"),
    [
        ("sneakers", DemoScenario.wrong_size, {"size"}),
        ("groceries", DemoScenario.substitution, {"name"}),
        ("electronics", DemoScenario.wrong_model, {"model"}),
        ("event tickets", DemoScenario.wrong_date, {"event_date"}),
        # the total stays put, so the unit price has to move with the quantity
        ("subscription", DemoScenario.wrong_quantity, {"quantity", "unit_price"}),
        # the old four still break only their own thing on a catalog mandate
        ("sneakers", DemoScenario.wrong_color, {"color"}),
        ("sneakers", DemoScenario.over_cap, {"unit_price"}),
        ("sneakers", DemoScenario.wrong_store, {"merchant"}),
    ],
)
def test_each_catalog_scenario_breaks_exactly_one_thing(category, scenario, changed):
    clean = build_for(category, DemoScenario.clean)

    other = build_for(category, scenario)

    assert changed_fields(clean, other) == changed


def test_wrong_size_is_the_nearest_size_the_store_stocks():
    # 9 gets 8.5, not 8. a half size off is what slips past a human
    assert build_for("sneakers", DemoScenario.wrong_size).items[0].size == "8.5"
    assert build_for("sneakers", DemoScenario.wrong_size, size="8").items[0].size == "8.5"


def test_wrong_size_keeps_the_words_around_the_number():
    demo_order = build_for("sneakers", DemoScenario.wrong_size, size="men's size 9")

    assert demo_order.items[0].size == "men's size 8.5"


def test_substitution_swaps_in_the_other_grocery():
    item = build_for("groceries", DemoScenario.substitution).items[0]

    assert item.name == "Organic almond milk"
    # the carton size is the mandate's, only the product changed
    assert item.size == "64 oz"


def test_substitution_skips_whatever_is_out_of_stock():
    rows = catalog_for("groceries")
    for row in rows:
        if row.name == "Organic almond milk":
            row.in_stock = False

    with pytest.raises(ScenarioNotPossible, match="nothing else in this category"):
        build_demo_order(catalog_mandate("groceries"), DemoScenario.substitution, rows)


def test_wrong_model_is_the_other_model_at_the_same_price():
    clean = build_for("electronics", DemoScenario.clean)

    wrong = build_for("electronics", DemoScenario.wrong_model)

    assert wrong.items[0].model == "FP-300"
    assert total_of(wrong) == total_of(clean)


def test_wrong_model_prefers_a_model_at_the_same_catalog_price():
    rows = catalog_for("electronics")
    # a pricier model listed first must lose to the one that costs the same as the FP-200
    pricier = CatalogItem(sku="FW-ELE-FP-100", name="Wireless earbuds", category="electronics",
                          model="FP-100", quantity_unit="pair", price=Decimal("129.00"),
                          in_stock=True, merchant="Fernwood Supply Co")
    rows.insert(0, pricier)

    demo_order = build_demo_order(
        catalog_mandate("electronics"), DemoScenario.wrong_model, rows
    )

    assert demo_order.items[0].model == "FP-300"


def test_wrong_date_is_the_same_event_the_next_day():
    item = build_for("event tickets", DemoScenario.wrong_date).items[0]

    assert item.event_date == date(2026, 12, 6)
    assert item.name == "Fall Makers Market general admission tickets"
    assert item_display_name(item) == "Fall Makers Market general admission tickets, Dec 6, 2026"


@pytest.mark.parametrize(
    ("category", "clean_quantity", "wrong_quantity"),
    [
        ("subscription", 1, 2),
        ("event tickets", 2, 3),
    ],
)
def test_wrong_quantity_is_one_more_for_the_same_money(category, clean_quantity, wrong_quantity):
    clean = build_for(category, DemoScenario.clean)

    wrong = build_for(category, DemoScenario.wrong_quantity)

    assert clean.items[0].quantity == clean_quantity
    assert wrong.items[0].quantity == wrong_quantity
    # a third of a total can be a cent short, never over. over could trip the cap
    assert total_of(clean) - Decimal("0.02") <= total_of(wrong) <= total_of(clean)


def test_wrong_quantity_unit_price_still_looks_like_a_price():
    # $28 for one month becomes $14 each for two, not $2.33 each for twelve
    wrong = build_for("subscription", DemoScenario.wrong_quantity).items[0]

    assert wrong.quantity == 2
    assert wrong.unit_price == Decimal("14.00")


def test_wrong_quantity_needs_no_catalog():
    demo_order = build_demo_order(make_mandate(), DemoScenario.wrong_quantity)

    assert demo_order.items[0].quantity == 2


@pytest.mark.parametrize(
    ("category", "rule_overrides", "scenario", "message"),
    [
        ("sneakers", {"size": None}, DemoScenario.wrong_size, "doesn't name a size"),
        ("electronics", {"model": None}, DemoScenario.wrong_model, "doesn't name a model"),
        ("event tickets", {"event_date": None}, DemoScenario.wrong_date,
         "doesn't name an event date"),
        # the parser can only ever be trusted to write a date, anything else can't be moved
        ("event tickets", {"event_date": "next Saturday"}, DemoScenario.wrong_date,
         "doesn't name an event date"),
        # one size of oat milk, one plan, one product of sneakers
        ("groceries", {}, DemoScenario.wrong_size, "no other size"),
        ("subscription", {}, DemoScenario.substitution, "nothing else in this category"),
        ("sneakers", {}, DemoScenario.substitution, "nothing else in this category"),
    ],
)
def test_catalog_scenario_with_nothing_to_break_refuses(
    category, rule_overrides, scenario, message
):
    with pytest.raises(ScenarioNotPossible, match=message):
        build_for(category, scenario, **rule_overrides)


@pytest.mark.parametrize(
    "scenario",
    [DemoScenario.wrong_size, DemoScenario.substitution, DemoScenario.wrong_model,
     DemoScenario.wrong_date],
)
def test_catalog_scenarios_refuse_a_mandate_thats_not_in_the_catalog(scenario):
    # the trail runners mandate has a size, but there's no catalog to pick another one from
    mandate = make_mandate(model="TR-1", event_date="2026-12-05")

    with pytest.raises(ScenarioNotPossible, match="isn't for anything in the store's catalog"):
        build_demo_order(mandate, scenario, [])


# ---- order items with a model and a date ----


def test_display_name_with_a_model():
    item = OrderItemIn(name="Wireless earbuds", model="FP-300", quantity=1, unit_price="79.00")

    assert item_display_name(item) == "Wireless earbuds, model FP-300"


def test_display_name_with_everything():
    item = OrderItemIn(name="Canvas low-top sneaker", color="white", size="9", model="CL-1",
                       event_date="2026-12-05", quantity=1, unit_price="64.00")

    assert item_display_name(item) == (
        "Canvas low-top sneaker, white, size 9, model CL-1, Dec 5, 2026"
    )


def test_an_event_date_that_isnt_a_date_is_refused():
    with pytest.raises(ValueError):
        OrderItemIn(name="Tickets", event_date="next Saturday", quantity=1, unit_price="25.00")


# ---- the catalog scenarios: the route ----


@pytest.fixture
def catalog_mandate_ids(sessions):
    # the real catalog rows, plus one mandate per category
    ids = {}
    with sessions() as session:
        for row in migration_catalog_rows():
            session.add(CatalogItem(**row))
        for category in CATALOG_MANDATES:
            mandate = catalog_mandate(category)
            session.add(mandate)
            session.flush()
            ids[category] = mandate.id
        session.commit()
    return ids


@pytest.mark.parametrize("category", list(CATALOG_MANDATES))
def test_clean_catalog_order_auto_captures(client, catalog_mandate_ids, paypal, category):
    order = place(client, catalog_mandate_ids[category], "clean").json()

    assert order["status"] == "captured"
    assert order["review_state"] == "auto_approved"


@pytest.mark.parametrize(
    ("category", "scenario", "field"),
    [
        ("sneakers", "wrong_size", "size"),
        ("groceries", "substitution", "substitution"),
        ("electronics", "wrong_model", "model"),
        ("event tickets", "wrong_date", "event_date"),
        ("subscription", "wrong_quantity", "quantity"),
    ],
)
def test_catalog_scenarios_hold_for_review_and_never_capture(
    client, catalog_mandate_ids, paypal, category, scenario, field
):
    response = place(client, catalog_mandate_ids[category], scenario)

    assert response.status_code == 201
    order = response.json()
    assert order["status"] == "authorized"
    assert order["review_state"] == "needs_review"
    # Claude caught it, not a rule. the total stayed under the cap in every one of these
    assert order["intent_check"]["checked_by"] == "ai"
    assert order["intent_check"]["mismatched_fields"] == [field]
    assert paypal.captured == []


def test_model_and_date_are_saved_and_sent_to_paypal(client, catalog_mandate_ids, paypal):
    earbuds = place(client, catalog_mandate_ids["electronics"], "wrong_model").json()
    tickets = place(client, catalog_mandate_ids["event tickets"], "wrong_date").json()

    assert earbuds["items"][0]["model"] == "FP-300"
    assert tickets["items"][0]["event_date"] == "2026-12-06"
    paypal_names = []
    for body in paypal.bodies:
        paypal_names.append(body["purchase_units"][0]["items"][0]["name"])
    assert paypal_names == [
        "Wireless earbuds, model FP-300",
        "Fall Makers Market general admission tickets, Dec 6, 2026",
    ]


def test_catalog_scenario_on_a_mandate_outside_the_catalog_is_a_422(
    client, catalog_mandate_ids, mandate_id, paypal
):
    # the trail runners mandate names a size, but the catalog sells no trail runners
    response = place(client, mandate_id, "wrong_size")

    assert response.status_code == 422
    assert response.json()["detail"] == "This mandate isn't for anything in the store's catalog"
    assert paypal.bodies == []


def test_twelve_months_at_the_catalog_price_trips_the_price_cap(
    client, catalog_mandate_ids, claude
):
    # the eval case: a year of the plan at the catalog's full monthly price, against a one-month
    # mandate with a $35 cap. the rule stops it, Claude never gets asked about the quantity
    response = client.post(
        "/orders",
        json={
            "mandate_id": catalog_mandate_ids["subscription"],
            "vault_id": VAULT_ID,
            "items": [{"name": "Pantry Box monthly plan", "quantity": 12, "unit_price": "29.00"}],
        },
        headers=AGENT_HEADERS,
    )

    assert response.status_code == 201
    assert response.json()["review_state"] == "needs_review"
    assert claude.calls == 0
