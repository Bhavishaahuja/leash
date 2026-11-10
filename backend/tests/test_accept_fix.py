# POST /review/{token}/accept-fix: void the order, place the suggested fix on the same saved
# wallet for the same agent, check it, auto-decide. PayPal is mocked at the HTTP level with
# `responses`, so the real PayPal client builds every request: the void path, the derived
# PayPal-Request-Id and the vault order body are what would really be sent.

import importlib.util
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import responses
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from keyed_client import MERCHANT_HEADERS
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import orders, review
from backend.app.config import get_settings
from backend.app.db import Base, get_session
from backend.app.models import Approval, ApprovalAction, CatalogItem, Mandate, Order
from backend.app.paypal_client import PayPalClient
from backend.app.rubber_stamp import get_fast_streak

BASE = "https://api-m.sandbox.paypal.com"
VAULT_ID = "test-vault-id"
WREN_KEY = "wren-test-key"
HOLD_EXPIRES = "2099-11-20T01:56:55Z"
ORIGINAL_AUTH = "AUTHORIGINAL0001"
REPLACEMENT_AUTH = "AUTHREPLACE00001"
MIGRATION_FILE = (
    Path(__file__).resolve().parents[1]
    / "migrations" / "versions" / "b91d6e2f4a73_add_catalog_items.py"
)

# the agent ordered a half size off. the catalog has the right one, white size 9 at $64.00
WRONG_SIZE_ORDER = {
    "mandate_id": 1,
    "vault_id": VAULT_ID,
    "items": [{"name": "Canvas low-top sneaker", "color": "white", "size": "8.5",
               "quantity": 1, "unit_price": "64.00"}],
    "shipping": "6.00",
}


def size_mismatch():
    return {"verdict": "mismatch", "risk_score": 85, "reason": "Ordered 8.5, mandate says 9",
            "mismatched_fields": ["size"]}


def clean_match():
    return {"verdict": "match", "risk_score": 5, "reason": "Matches.", "mismatched_fields": []}


class FakeClaude:
    # answers the order checks in turn: the original first, then the replacement
    def __init__(self):
        self.answers = [size_mismatch(), clean_match()]
        self.calls = 0
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def with_options(self, **options):
        return self

    def _create(self, **kwargs):
        self.calls += 1
        answer = self.answers.pop(0)
        block = SimpleNamespace(type="tool_use", name=kwargs["tools"][0]["name"], input=answer)
        return SimpleNamespace(stop_reason="tool_use", content=[block])


def authorized_paypal_order(order_id, authorization_id):
    # what a vault_id + AUTHORIZE create returns, per the Orders v2 vaulted example: COMPLETED
    # with the authorization already inside
    return {
        "id": order_id,
        "status": "COMPLETED",
        "purchase_units": [{"payments": {"authorizations": [{
            "id": authorization_id, "status": "CREATED",
            "amount": {"currency_code": "USD", "value": "70.00"},
            "expiration_time": HOLD_EXPIRES,
        }]}}],
    }


def catalog_rows():
    spec = importlib.util.spec_from_file_location("catalog_migration", MIGRATION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.catalog_rows()


@pytest.fixture
def paypal():
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        mock.add(responses.POST, BASE + "/v1/oauth2/token",
                 json={"access_token": "test-token", "expires_in": 32400})
        # the original order, then the replacement. responses hands these out in turn
        mock.add(responses.POST, BASE + "/v2/checkout/orders", status=201,
                 json=authorized_paypal_order("PPORIGINAL", ORIGINAL_AUTH))
        mock.add(responses.POST, BASE + "/v2/checkout/orders", status=201,
                 json=authorized_paypal_order("PPREPLACE", REPLACEMENT_AUTH))
        # Payments v2 void: 204 with no body unless Prefer asks for the representation
        mock.add(responses.POST, BASE + "/v2/payments/authorizations/" + ORIGINAL_AUTH + "/void",
                 status=200, json={"id": ORIGINAL_AUTH, "status": "VOIDED"})
        mock.add(responses.POST,
                 BASE + "/v2/payments/authorizations/" + REPLACEMENT_AUTH + "/capture",
                 status=201, json={"id": "CAPREPLACE", "status": "COMPLETED"})
        yield mock


@pytest.fixture
def sessions():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    with maker() as session:
        for row in catalog_rows():
            session.add(CatalogItem(**row))
        session.add(Mandate(
            id=1, buyer_label="Sam",
            raw_request="White canvas sneakers in a size 9, nothing over $80, from Fernwood.",
            rules={"item": "canvas sneakers", "category": "sneakers", "color": "white",
                   "size": "9", "quantity": 1, "currency": "USD"},
            max_price=Decimal("80.00"), merchant="Fernwood Supply Co",
        ))
        session.commit()
    return maker


@pytest.fixture
def claude():
    return FakeClaude()


@pytest.fixture
def client(sessions, paypal, claude, monkeypatch):
    monkeypatch.setenv("AGENT_API_KEYS", WREN_KEY + ":Wren")
    get_settings.cache_clear()

    app = FastAPI()
    app.include_router(orders.router)
    app.include_router(review.router)

    def test_session():
        session = sessions()
        try:
            yield session
        finally:
            session.close()

    paypal_client = PayPalClient("id", "secret", base_url=BASE)
    app.dependency_overrides[get_session] = test_session
    app.dependency_overrides[orders.get_paypal_client] = lambda: paypal_client
    app.dependency_overrides[orders.get_anthropic_client] = lambda: claude
    return TestClient(app)


def place_wrong_size_order(client):
    response = client.post("/orders", json=WRONG_SIZE_ORDER, headers={"X-API-Key": WREN_KEY})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def review_link(client, order_id):
    response = client.post("/orders/" + str(order_id) + "/review-link", headers=MERCHANT_HEADERS)
    assert response.status_code == 201, response.text
    return "/review/" + response.json()["review_token"]


@pytest.fixture
def held(client):
    # an order waiting for a human, with a suggested fix on it, and its open review link
    order_id = place_wrong_size_order(client)
    link = review_link(client, order_id)
    assert client.post(link + "/opened").status_code == 204
    return SimpleNamespace(order_id=order_id, link=link)


def accept(client, link, decision_ms=3100):
    return client.post(link + "/accept-fix", json={"decision_ms": decision_ms})


def paypal_calls(paypal, url_part):
    found = []
    for call in paypal.calls:
        if url_part in call.request.url:
            found.append(call)
    return found


def load(sessions, order_id):
    with sessions() as session:
        return session.get(Order, order_id)


# ---- the swap ----


def test_the_held_order_has_a_fix_to_accept(client, held):
    view = client.get(held.link).json()

    assert view["intent_check"]["suggested_fix"]["sku"] == "FW-SNK-WHI-9"


def test_accepting_voids_the_original_and_places_the_fix(client, sessions, paypal, held):
    response = accept(client, held.link)

    assert response.status_code == 200, response.text
    body = response.json()
    replacement_id = body["replacement"]["id"]
    assert body["order"]["status"] == "voided"
    assert body["order"]["replaced_by_order_id"] == replacement_id
    # the replacement went through the check and auto-decide like any order: a clean match
    assert body["replacement"]["status"] == "captured"
    assert body["replacement"]["review_state"] == "auto_approved"
    assert body["replacement"]["items"][0]["size"] == "9"

    old = load(sessions, held.order_id)
    new = load(sessions, replacement_id)
    assert old.replaced_by_order_id == new.id
    assert new.replaces_order_id == old.id
    # same agent, same saved wallet, same mandate and shipping
    assert new.agent_name == "Wren"
    assert new.vault_id == VAULT_ID
    assert new.mandate_id == old.mandate_id
    assert new.shipping_amount == old.shipping_amount
    assert new.total == Decimal("70.00")


def test_the_void_is_the_real_payments_v2_call(paypal, sessions, client, held):
    accept(client, held.link)

    voids = paypal_calls(paypal, "/void")
    assert len(voids) == 1
    assert voids[0].request.url == (
        BASE + "/v2/payments/authorizations/" + ORIGINAL_AUTH + "/void"
    )
    # derived, so a retry can't void twice, and the same id a plain reject would send
    expected = orders.payment_request_id(load(sessions, held.order_id), orders.VOID)
    assert voids[0].request.headers["PayPal-Request-Id"] == expected


def test_the_replacement_goes_on_the_saved_wallet_with_no_buyer_popup(paypal, client, held):
    accept(client, held.link)

    creates = paypal_calls(paypal, "/v2/checkout/orders")
    assert len(creates) == 2
    sent = json.loads(creates[1].request.body)
    assert sent["intent"] == "AUTHORIZE"
    assert sent["payment_source"]["paypal"]["vault_id"] == VAULT_ID
    assert sent["payment_source"]["paypal"]["stored_credential"]["payment_initiator"] == "MERCHANT"
    item = sent["purchase_units"][0]["items"][0]
    assert item["name"] == "Canvas low-top sneaker, white, size 9"
    # the catalog's price, not anything the agent sent
    assert item["unit_amount"]["value"] == "64.00"
    # a new order gets a fresh request id, never the original's
    assert (creates[1].request.headers["PayPal-Request-Id"]
            != creates[0].request.headers["PayPal-Request-Id"])


def test_the_replacement_is_checked_again(client, claude, held):
    accept(client, held.link)

    assert claude.calls == 2


def test_a_replacement_the_check_doubts_goes_to_a_human(client, claude, paypal, held):
    claude.answers = [{"verdict": "unsure", "risk_score": 40, "reason": "Can't tell.",
                       "mismatched_fields": []}]

    body = accept(client, held.link).json()

    assert body["replacement"]["status"] == "authorized"
    assert body["replacement"]["review_state"] == "needs_review"
    assert paypal_calls(paypal, "/capture") == []


def test_the_decision_is_recorded_as_accept_fix_with_server_timing(client, sessions, held):
    accept(client, held.link, decision_ms=1)

    with sessions() as session:
        approval = session.query(Approval).filter_by(order_id=held.order_id).one()
        assert approval.action == ApprovalAction.accept_fix
        # the server's own clock, opened to tap, not the 1 the client claimed
        elapsed = approval.decided_at - approval.opened_at
        assert approval.decision_ms == int(elapsed.total_seconds() * 1000)
        assert approval.client_decision_ms == 1


def test_accepting_a_fix_breaks_a_rubber_stamp_streak():
    # it's a considered choice, not a reflex approve
    decisions = [SimpleNamespace(action="accept_fix", decision_ms=500),
                 SimpleNamespace(action="approve", decision_ms=500)]

    assert get_fast_streak(decisions) == 0


# ---- the link is still single-use ----


@pytest.mark.parametrize("second", ["accept-fix", "approve", "reject"])
def test_the_link_cant_be_used_again(client, paypal, held, second):
    accept(client, held.link)

    response = client.post(held.link + "/" + second, json={"decision_ms": 100})

    assert response.status_code == 409
    assert len(paypal_calls(paypal, "/void")) == 1
    assert len(paypal_calls(paypal, "/v2/checkout/orders")) == 2


def test_an_unknown_link_is_a_404(client, paypal):
    assert client.post("/review/made-up-token/accept-fix").status_code == 404
    assert paypal_calls(paypal, "/void") == []


# ---- refused before any money moves ----


def set_on_order(sessions, order_id, **values):
    with sessions() as session:
        order = session.get(Order, order_id)
        for name, value in values.items():
            setattr(order, name, value)
        session.commit()


def set_fix(sessions, order_id, fix):
    with sessions() as session:
        decision = session.get(Order, order_id).intent_check
        decision.suggested_fix = fix
        session.commit()


def assert_nothing_moved(client, sessions, paypal, held):
    order = load(sessions, held.order_id)
    assert order.status.value == "authorized"
    assert order.review_state.value == "needs_review"
    assert order.payment_claimed_at is None
    assert order.replaced_by_order_id is None
    assert paypal_calls(paypal, "/void") == []
    assert len(paypal_calls(paypal, "/v2/checkout/orders")) == 1


def test_no_fix_is_a_409_and_the_link_still_works(client, sessions, paypal, held):
    set_fix(sessions, held.order_id, None)

    response = accept(client, held.link)

    assert response.status_code == 409
    assert response.json()["detail"]["message"] == "There's no suggested fix for this order"
    assert_nothing_moved(client, sessions, paypal, held)
    # the human can still reject it the normal way
    assert client.post(held.link + "/reject", json={"decision_ms": 900}).status_code == 200


def test_an_order_without_a_saved_wallet_cant_be_replaced(client, sessions, paypal, held):
    # paid through the PayPal popup: no buyer is here to approve a new payment
    set_on_order(sessions, held.order_id, vault_id=None)

    response = accept(client, held.link)

    assert response.status_code == 409
    assert "saved wallet" in response.json()["detail"]["message"]
    assert_nothing_moved(client, sessions, paypal, held)


def test_a_fix_thats_sold_out_now_is_refused(client, sessions, paypal, held):
    with sessions() as session:
        session.query(CatalogItem).filter_by(sku="FW-SNK-WHI-9").one().in_stock = False
        session.commit()

    response = accept(client, held.link)

    assert response.status_code == 409
    assert response.json()["detail"]["message"] == "The suggested item isn't in stock any more"
    assert_nothing_moved(client, sessions, paypal, held)


def test_a_fix_whose_price_changed_is_refused(client, sessions, paypal, held):
    # the human agreed to $64.00. they haven't agreed to $69.00
    with sessions() as session:
        session.query(CatalogItem).filter_by(sku="FW-SNK-WHI-9").one().price = Decimal("69.00")
        session.commit()

    response = accept(client, held.link)

    assert response.status_code == 409
    assert "price changed" in response.json()["detail"]["message"]
    assert_nothing_moved(client, sessions, paypal, held)


# ---- PayPal failing ----


def test_a_failed_void_changes_nothing_and_can_be_retried(client, sessions, paypal, held):
    paypal.replace(responses.POST,
                   BASE + "/v2/payments/authorizations/" + ORIGINAL_AUTH + "/void",
                   status=500, json={"name": "INTERNAL_SERVER_ERROR"})

    response = accept(client, held.link)

    assert response.status_code == 502
    assert response.json()["detail"]["status"] == "authorized"
    order = load(sessions, held.order_id)
    assert order.status.value == "authorized"
    assert order.payment_claimed_at is None
    # never got as far as a new order
    assert len(paypal_calls(paypal, "/v2/checkout/orders")) == 1

    paypal.replace(responses.POST,
                   BASE + "/v2/payments/authorizations/" + ORIGINAL_AUTH + "/void",
                   status=204)
    assert accept(client, held.link).status_code == 200


def test_a_void_with_no_body_counts_as_voided(client, sessions, paypal, held):
    # Payments v2's default, Prefer: return=minimal, is a 204 with nothing in it
    paypal.replace(responses.POST,
                   BASE + "/v2/payments/authorizations/" + ORIGINAL_AUTH + "/void",
                   status=204)

    assert accept(client, held.link).status_code == 200
    assert load(sessions, held.order_id).void_result is not None


def test_replacement_failing_at_paypal_leaves_the_original_voided(client, sessions, paypal, held):
    # the create for the replacement is refused
    paypal.replace(responses.POST, BASE + "/v2/checkout/orders", status=422,
                   json={"name": "UNPROCESSABLE_ENTITY", "debug_id": "dbg123",
                         "details": [{"issue": "PAYER_ACTION_REQUIRED"}]})

    response = accept(client, held.link)

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["message"].startswith("The original hold was voided, but the replacement order "
                                        "failed. Nothing was charged.")
    assert detail["voided_order_id"] == held.order_id
    assert detail["status"] == "voided"
    assert detail["paypal"]["debug_id"] == "dbg123"

    # the original stays voided, and points at the failed attempt
    old = load(sessions, held.order_id)
    assert old.status.value == "voided"
    new = load(sessions, detail["replacement_order_id"])
    assert new.status.value == "failed"
    assert new.replaces_order_id == old.id
    assert old.replaced_by_order_id == new.id
    assert paypal_calls(paypal, "/capture") == []


def test_replacement_paypal_never_authorizes_is_reported_too(client, sessions, paypal, held):
    # PayPal takes the order but no authorization comes back, even after /authorize
    paypal.replace(responses.POST, BASE + "/v2/checkout/orders", status=201,
                   json={"id": "PPREPLACE", "status": "CREATED"})
    paypal.add(responses.POST, BASE + "/v2/checkout/orders/PPREPLACE/authorize", status=201,
               json={"id": "PPREPLACE", "status": "CREATED"})

    response = accept(client, held.link)

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["status"] == "voided"
    assert load(sessions, detail["replacement_order_id"]).status.value == "failed"
    assert load(sessions, held.order_id).status.value == "voided"


def test_replacement_crashing_on_our_side_is_reported_and_the_void_stays(
    client, sessions, paypal, held, monkeypatch
):
    def broken(*args, **kwargs):
        raise RuntimeError("database fell over")

    monkeypatch.setattr(orders, "place_order", broken)

    response = accept(client, held.link)

    assert response.status_code == 502
    assert response.json()["detail"]["replacement_order_id"] is None
    assert load(sessions, held.order_id).status.value == "voided"


# ---- the migration ----


def test_migration_allows_accept_fix_and_downgrades_it_to_reject(tmp_path):
    url = "sqlite:///" + str(tmp_path / "test.db")
    alembic_config = Config("alembic.ini")
    alembic_config.set_main_option("sqlalchemy.url", url)
    command.upgrade(alembic_config, "head")
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO mandates (id, buyer_label, raw_request, rules, max_price_inclusive) "
            "VALUES (1, 'Sam', 'x', '{}', 1)"
        ))
        connection.execute(text(
            "INSERT INTO orders (id, mandate_id, items, total, currency, status, watch_mode) "
            "VALUES (1, 1, '[]', 1, 'USD', 'voided', 'normal')"
        ))
        connection.execute(text(
            "INSERT INTO approvals (order_id, action) VALUES (1, 'accept_fix')"
        ))

    command.downgrade(alembic_config, "d7f2a9c4e183")

    with engine.connect() as connection:
        action = connection.execute(text("SELECT action FROM approvals")).scalar_one()
    assert action == "reject"
