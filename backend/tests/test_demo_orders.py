# POST /demo/orders and GET /mandates. The builder needs no fakes at all. The route tests go through
# the real place_order with PayPal and Claude faked, so nothing here reaches the network.

import uuid
from decimal import Decimal
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
from backend.app.models import Mandate, Order
from backend.app.orders import item_display_name

VAULT_ID = "test-demo-vault"


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
    # the one call it can get here is the order check. it reads the color like the real one would
    def __init__(self):
        self.calls = 0
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def with_options(self, **options):
        return self

    def _create(self, **kwargs):
        self.calls += 1
        if '"color": "black"' in kwargs["messages"][0]["content"].split("<order>")[1]:
            fields = {"verdict": "match", "risk_score": 5, "reason": "Matches.",
                      "mismatched_fields": []}
        else:
            fields = {"verdict": "mismatch", "risk_score": 90, "reason": "Wrong color.",
                      "mismatched_fields": ["color"]}
        block = SimpleNamespace(type="tool_use", name=kwargs["tools"][0]["name"], input=fields)
        return SimpleNamespace(stop_reason="tool_use", content=[block])


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
