# Order endpoints with PayPal mocked (responses) and SQLite. The PayPal bodies are trimmed
# from the Orders v2 / Payments v2 schema examples and what spikes 1 + 2 got back in sandbox

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import requests
import responses
from fastapi import FastAPI
from fastapi.testclient import TestClient
from keyed_client import AGENT_HEADERS, MERCHANT_HEADERS, KeyedClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import orders, review
from backend.app.auth import issue_session_token
from backend.app.config import get_settings
from backend.app.db import Base, get_session
from backend.app.models import (
    Approval,
    ApprovalAction,
    CheckedBy,
    Decision,
    Mandate,
    Order,
    OrderStatus,
    ReviewState,
    Verdict,
)
from backend.app.paypal_client import PayPalClient

BASE = "https://api-m.sandbox.paypal.com"
VAULT_ID = "test-vault-id"
# far off, because review links die with the hold and a real date would break these tests one day
HOLD_EXPIRES = "2099-11-20T01:56:55Z"

ORDER_BODY = {
    "mandate_id": 1,
    "items": [
        {"name": "Trail runner", "color": "black", "size": "9", "quantity": 1,
         "unit_price": "42.00"},
        {"name": "Wool hiking socks", "quantity": 2, "unit_price": "9.50"},
    ],
    "shipping": "5.00",
}


class FakeClaude:
    # stands in for the Anthropic client. without it these tests would really call Claude on any
    # machine with a key in .env. answers unsure unless a test says otherwise, so an order only
    # auto-captures in the tests that ask for it

    def __init__(self):
        self.answer = {
            "verdict": "unsure",
            "risk_score": 50,
            "reason": "Can't tell from the request.",
            "mismatched_fields": [],
        }
        self.error = None
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def with_options(self, **options):
        return self

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        if self.error:
            raise self.error
        block = SimpleNamespace(type="tool_use", name="record_intent_check", input=self.answer)
        return SimpleNamespace(stop_reason="tool_use", content=[block])


def match_answer(risk_score=5):
    return {
        "verdict": "match",
        "risk_score": risk_score,
        "reason": "Order matches the mandate.",
        "mismatched_fields": [],
    }


def authorized_paypal_order(order_id, authorization_id, amount="66.00", status="CREATED"):
    # what a vault_id + AUTHORIZE create (or an /authorize call) returns: COMPLETED,
    # with the authorization already inside
    return {
        "id": order_id,
        "status": "COMPLETED",
        "purchase_units": [{
            "payments": {
                "authorizations": [{
                    "id": authorization_id,
                    "status": status,
                    "amount": {"currency_code": "USD", "value": amount},
                    "expiration_time": HOLD_EXPIRES,
                }]
            }
        }],
    }


def payer_action_paypal_order(order_id):
    return {
        "id": order_id,
        "status": "PAYER_ACTION_REQUIRED",
        "links": [
            {"href": BASE + "/v2/checkout/orders/" + order_id, "rel": "self", "method": "GET"},
            {"href": "https://www.sandbox.paypal.com/checkoutnow?token=" + order_id,
             "rel": "payer-action", "method": "GET"},
        ],
    }


@pytest.fixture
def paypal():
    # tests that never reach PayPal (404s, 409s) won't use the token mock, that's fine
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mock:
        mock.add(
            responses.POST,
            BASE + "/v1/oauth2/token",
            json={"access_token": "test-token", "expires_in": 32400},
        )
        yield mock


@pytest.fixture
def db_sessions():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as session:
        session.add(Mandate(
            id=1,
            buyer_label="Sam",
            raw_request="Trail runners and socks under $100 from Fernwood Supply Co",
            rules={"max_price": 100},
            max_price=Decimal("100.00"),
            merchant="Fernwood Supply Co",
        ))
        # the wrong-store demo: any order against this one fails the hard rules
        session.add(Mandate(
            id=2,
            buyer_label="Sam",
            raw_request="Trail runners from Ridgeline Outdoors",
            rules={},
            max_price=Decimal("100.00"),
            merchant="Ridgeline Outdoors",
        ))
        session.commit()
    return sessions


@pytest.fixture
def claude():
    return FakeClaude()


@pytest.fixture
def app(db_sessions, paypal, claude):
    app = FastAPI()
    app.include_router(orders.router)
    app.include_router(orders.agent_router)
    app.include_router(review.router)

    def test_session():
        session = db_sessions()
        try:
            yield session
        finally:
            session.close()

    paypal_client = PayPalClient("id", "secret", base_url=BASE)
    app.dependency_overrides[get_session] = test_session
    app.dependency_overrides[orders.get_paypal_client] = lambda: paypal_client
    app.dependency_overrides[orders.get_anthropic_client] = lambda: claude
    return app


@pytest.fixture
def client(app):
    return KeyedClient(app)


@pytest.fixture
def no_key_client(app):
    # sends only the headers a test gives it
    return TestClient(app)


def sent_json(paypal, url_part):
    # body of the last request we sent to a PayPal URL containing url_part
    for call in reversed(paypal.calls):
        if url_part in call.request.url and call.request.body:
            return json.loads(call.request.body)
    return None


def merchant_view(client, placed):
    # the agent only gets four fields back. most tests want to look at the whole order, so this
    # reads it the way the dashboard would
    order = client.get("/orders/" + str(placed["id"])).json()
    order["payer_action_url"] = placed.get("payer_action_url")
    return order


def create_vault_order(client, paypal, paypal_order_id="5O190127TN364715T",
                       authorization_id="0AW2184448108334S"):
    paypal.add(
        responses.POST,
        BASE + "/v2/checkout/orders",
        status=201,
        json=authorized_paypal_order(paypal_order_id, authorization_id),
    )
    body = dict(ORDER_BODY)
    body["vault_id"] = VAULT_ID
    response = client.post("/orders", json=body)
    assert response.status_code == 201, response.text
    return merchant_view(client, response.json())


def create_unapproved_order(client, paypal, paypal_order_id="8XU44448NV0000000"):
    paypal.add(
        responses.POST,
        BASE + "/v2/checkout/orders",
        status=201,
        json=payer_action_paypal_order(paypal_order_id),
    )
    response = client.post("/orders", json=ORDER_BODY)
    assert response.status_code == 201, response.text
    return merchant_view(client, response.json())


def mock_capture(paypal, authorization_id="0AW2184448108334S", capture_id="2GG279541U471931P"):
    paypal.add(
        responses.POST,
        BASE + "/v2/payments/authorizations/" + authorization_id + "/capture",
        status=201,
        json={"id": capture_id, "status": "COMPLETED"},
    )


def mock_void(paypal, authorization_id="0AW2184448108334S"):
    paypal.add(
        responses.POST,
        BASE + "/v2/payments/authorizations/" + authorization_id + "/void",
        status=204,
    )


def mint_link(client, order):
    return client.post("/orders/" + str(order["id"]) + "/review-link")


# one link per order per test, kept here so a second tap really does reuse the first one's token.
# keyed by the client too, since each test gets a new one with order ids starting over
review_tokens = {}


def tap(client, order, action, decision_ms):
    key = (id(client), order["id"])
    if key not in review_tokens:
        link = mint_link(client, order)
        if link.status_code != 201:
            # the order can't be reviewed, and the conflict has the same shape a tap's would
            return link
        review_tokens[key] = link.json()["review_token"]
    return client.post(
        "/review/" + review_tokens[key] + "/" + action, json={"decision_ms": decision_ms}
    )


def approve(client, order, decision_ms=4200):
    return tap(client, order, "approve", decision_ms)


def reject(client, order, decision_ms=4200):
    return tap(client, order, "reject", decision_ms)


def set_on_order(db_sessions, order, **values):
    with db_sessions() as session:
        saved = session.get(Order, order["id"])
        for name, value in values.items():
            setattr(saved, name, value)
        session.commit()


def paypal_calls_to(paypal, url_part):
    found = []
    for call in paypal.calls:
        if url_part in call.request.url:
            found.append(call)
    return found


def saved_order(db_sessions, order_id):
    with db_sessions() as session:
        return session.get(Order, order_id)


# ---- happy paths ----


def test_vault_order_is_authorized_then_approved(client, paypal):
    order = create_vault_order(client, paypal)

    assert order["status"] == "authorized"
    assert order["review_state"] == "needs_review"
    assert order["paypal_order_id"] == "5O190127TN364715T"
    assert order["authorization_id"] == "0AW2184448108334S"
    assert order["authorization_expires_at"].startswith("2099-11-20T01:56:55")
    assert order["vault_id"] == VAULT_ID
    assert order["payer_action_url"] is None
    # 42.00 + 2 x 9.50 + 5.00 shipping
    assert order["total"] == "66.00"
    assert order["shipping_amount"] == "5.00"

    # what we sent PayPal: spike 2's vault shape, and amounts that add up
    sent = sent_json(paypal, "/v2/checkout/orders")
    assert sent["intent"] == "AUTHORIZE"
    assert sent["payment_source"]["paypal"]["vault_id"] == VAULT_ID
    assert sent["payment_source"]["paypal"]["stored_credential"]["payment_initiator"] == "MERCHANT"
    unit = sent["purchase_units"][0]
    assert unit["amount"]["value"] == "66.00"
    assert unit["amount"]["breakdown"]["item_total"]["value"] == "61.00"
    assert unit["amount"]["breakdown"]["shipping"]["value"] == "5.00"
    assert unit["items"][0]["name"] == "Trail runner, black, size 9"
    assert unit["items"][1]["quantity"] == "2"
    assert unit["custom_id"] == str(order["id"])

    mock_capture(paypal)
    captured = approve(client, order)

    assert captured.status_code == 200
    assert captured.json()["status"] == "captured"
    assert captured.json()["review_state"] == "approved"
    detail = client.get("/orders/" + str(order["id"])).json()
    assert detail["capture_id"] == "2GG279541U471931P"


def test_shipping_amount_is_saved_on_the_row(client, paypal, db_sessions):
    order = create_vault_order(client, paypal)

    with db_sessions() as session:
        saved = session.get(Order, order["id"])
        assert saved.shipping_amount == Decimal("5.00")
        assert saved.total == Decimal("66.00")


def test_shipping_defaults_to_zero(client, paypal):
    paypal.add(
        responses.POST,
        BASE + "/v2/checkout/orders",
        status=201,
        json=authorized_paypal_order("5O190127TN364715T", "0AW2184448108334S", "61.00"),
    )
    body = dict(ORDER_BODY)
    body["vault_id"] = VAULT_ID
    del body["shipping"]

    response = client.post("/orders", json=body)

    assert response.status_code == 201
    order = merchant_view(client, response.json())
    assert order["shipping_amount"] == "0.00"
    assert order["total"] == "61.00"


def test_vault_order_then_rejected(client, paypal):
    order = create_vault_order(client, paypal)

    mock_void(paypal)
    voided = reject(client, order)

    assert voided.status_code == 200
    assert voided.json()["status"] == "voided"
    assert voided.json()["review_state"] == "rejected"


def test_vault_order_falls_back_to_authorize(client, paypal):
    # spike 2's fallback: if create ever comes back without an authorization, authorize it
    paypal.add(
        responses.POST,
        BASE + "/v2/checkout/orders",
        status=201,
        json={"id": "5O190127TN364715T", "status": "APPROVED"},
    )
    paypal.add(
        responses.POST,
        BASE + "/v2/checkout/orders/5O190127TN364715T/authorize",
        status=201,
        json=authorized_paypal_order("5O190127TN364715T", "0AW2184448108334S"),
    )
    body = dict(ORDER_BODY)
    body["vault_id"] = VAULT_ID

    response = client.post("/orders", json=body)

    assert response.json()["status"] == "authorized"
    assert merchant_view(client, response.json())["authorization_id"] == "0AW2184448108334S"


def test_no_vault_returns_payer_action_link_then_authorizes(client, paypal):
    order = create_unapproved_order(client, paypal)

    assert order["status"] == "created"
    assert order["authorization_id"] is None
    assert order["payer_action_url"] == (
        "https://www.sandbox.paypal.com/checkoutnow?token=8XU44448NV0000000"
    )
    sent = sent_json(paypal, "/v2/checkout/orders")
    assert "vault_id" not in sent["payment_source"]["paypal"]
    assert sent["payment_source"]["paypal"]["experience_context"]["brand_name"] == (
        "Fernwood Supply Co"
    )

    # buyer approved in the browser, now place the hold
    paypal.add(
        responses.POST,
        BASE + "/v2/checkout/orders/8XU44448NV0000000/authorize",
        status=201,
        json=authorized_paypal_order("8XU44448NV0000000", "7PS21555RT234531W"),
    )
    authorized = client.post("/orders/" + str(order["id"]) + "/authorize")

    assert authorized.status_code == 200
    assert authorized.json()["status"] == "authorized"
    assert merchant_view(client, authorized.json())["authorization_id"] == "7PS21555RT234531W"


# ---- the intent check ----


def saved_decisions(db_sessions):
    with db_sessions() as session:
        return session.query(Decision).order_by(Decision.id).all()


def post_vault_order(client, paypal, mandate_id=1, status="CREATED"):
    paypal.add(
        responses.POST,
        BASE + "/v2/checkout/orders",
        status=201,
        json=authorized_paypal_order("5O190127TN364715T", "0AW2184448108334S", status=status),
    )
    body = dict(ORDER_BODY)
    body["vault_id"] = VAULT_ID
    body["mandate_id"] = mandate_id
    return client.post("/orders", json=body)


def test_authorized_order_is_checked_and_the_response_has_the_result(client, paypal, claude,
                                                                     db_sessions):
    claude.answer = {
        "verdict": "mismatch",
        "risk_score": 85,
        "reason": "Ordered red, mandate says black",
        "mismatched_fields": ["color"],
    }

    order = create_vault_order(client, paypal)

    check = order["intent_check"]
    assert check["verdict"] == "mismatch"
    assert check["risk_score"] == 85
    assert check["reason"] == "Ordered red, mandate says black"
    assert check["mismatched_fields"] == ["color"]
    assert check["checked_by"] == "ai"
    assert isinstance(check["latency_ms"], int)
    assert len(check["rule_results"]) == 4
    assert check["rule_results"][0]["rule"] == "price"
    assert check["rule_results"][0]["passed"] is True
    assert check["rule_results"][0]["reason"] == (
        "Order is $66.00 ($61.00 items + $5.00 shipping), within the $100.00 cap"
    )
    assert len(claude.requests) == 1

    # the response is what got saved, not a separate copy of it
    decisions = saved_decisions(db_sessions)
    assert len(decisions) == 1
    assert decisions[0].id == check["id"]
    assert decisions[0].order_id == order["id"]
    assert decisions[0].risk_score == 85
    assert decisions[0].mismatched_fields == ["color"]
    assert decisions[0].latency_ms == check["latency_ms"]
    assert decisions[0].rule_results[0]["rule"] == "price"


def test_hard_rule_failure_saves_without_calling_claude(client, paypal, claude, db_sessions):
    response = post_vault_order(client, paypal, mandate_id=2)

    assert response.status_code == 201
    check = merchant_view(client, response.json())["intent_check"]
    assert check["verdict"] == "mismatch"
    assert check["checked_by"] == "rules"
    assert check["risk_score"] == 100
    assert check["reason"] == "Mandate says Ridgeline Outdoors, order is at Fernwood Supply Co"
    assert check["mismatched_fields"] == ["merchant"]
    assert claude.requests == []
    assert saved_decisions(db_sessions)[0].checked_by == CheckedBy.rules
    # flagged but not voided, the human hasn't been asked yet
    assert response.json()["status"] == "authorized"


def test_no_vault_order_is_checked_at_authorize_not_create(client, paypal, claude, db_sessions):
    order = create_unapproved_order(client, paypal)

    # nothing's authorized yet, so nothing to check
    assert order["intent_check"] is None
    assert claude.requests == []
    assert saved_decisions(db_sessions) == []

    paypal.add(
        responses.POST,
        BASE + "/v2/checkout/orders/8XU44448NV0000000/authorize",
        status=201,
        json=authorized_paypal_order("8XU44448NV0000000", "7PS21555RT234531W"),
    )
    authorized = client.post("/orders/" + str(order["id"]) + "/authorize")

    assert authorized.status_code == 200
    assert merchant_view(client, authorized.json())["intent_check"]["verdict"] == "unsure"
    assert len(claude.requests) == 1
    assert len(saved_decisions(db_sessions)) == 1


def test_failed_authorize_skips_the_check(client, paypal, claude, db_sessions):
    order = create_unapproved_order(client, paypal)
    paypal.add(
        responses.POST,
        BASE + "/v2/checkout/orders/8XU44448NV0000000/authorize",
        status=422,
        json={"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_NOT_APPROVED"}]},
    )

    response = client.post("/orders/" + str(order["id"]) + "/authorize")

    assert response.status_code == 502
    assert claude.requests == []
    assert saved_decisions(db_sessions) == []


def test_failed_create_skips_the_check(client, paypal, claude, db_sessions):
    paypal.add(
        responses.POST,
        BASE + "/v2/checkout/orders",
        status=422,
        json={"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "PAYEE_ACCOUNT_RESTRICTED"}]},
    )
    body = dict(ORDER_BODY)
    body["vault_id"] = VAULT_ID

    response = client.post("/orders", json=body)

    assert response.status_code == 502
    assert claude.requests == []
    assert saved_decisions(db_sessions) == []


def test_denied_authorization_skips_the_check(client, paypal, claude, db_sessions):
    # PayPal answered fine, but there's no hold on the money
    response = post_vault_order(client, paypal, status="DENIED")

    assert response.status_code == 201
    assert response.json()["status"] == "failed"
    assert merchant_view(client, response.json())["intent_check"] is None
    assert claude.requests == []
    assert saved_decisions(db_sessions) == []


def test_claude_being_down_doesnt_fail_the_order(client, paypal, claude, db_sessions):
    claude.error = RuntimeError("Claude is having a day")

    order = create_vault_order(client, paypal)

    assert order["status"] == "authorized"
    assert order["intent_check"]["verdict"] == "unsure"
    assert order["intent_check"]["risk_score"] == 50
    assert order["intent_check"]["reason"] == "Couldn't verify automatically, please review"
    assert len(saved_decisions(db_sessions)) == 1


def test_failed_save_still_returns_the_authorized_order(client, paypal, db_sessions, monkeypatch):
    # an error here could make the agent buy again and place a second hold
    def broken_build(order, result):
        raise SQLAlchemyError("database went away")

    monkeypatch.setattr(orders, "build_decision", broken_build)

    order = create_vault_order(client, paypal)

    assert order["status"] == "authorized"
    assert order["authorization_id"] == "0AW2184448108334S"
    assert order["intent_check"] is None
    assert saved_decisions(db_sessions) == []


def test_no_transaction_is_held_during_the_check(db_sessions, monkeypatch):
    # Claude can take 20 seconds, and on Postgres an open transaction would keep a connection
    # out of the pool that whole time, so the check has to run with none open. also proves the
    # check doesn't lazy-load anything, which would quietly open one again
    seen = {}
    real_check = orders.run_intent_check

    def spying_check(mandate, order, client=None, sharp=False):
        seen["before"] = session.in_transaction()
        result = real_check(mandate, order, client=client, sharp=sharp)
        seen["after"] = session.in_transaction()
        return result

    monkeypatch.setattr(orders, "run_intent_check", spying_check)

    with db_sessions() as session:
        order = Order(
            mandate_id=1,
            items=[{"name": "Trail runner", "quantity": 1, "unit_price": "42.00"}],
            total=Decimal("42.00"),
            shipping_amount=Decimal("0.00"),
            currency="USD",
        )
        session.add(order)
        session.commit()

        orders.check_and_save(session, order, FakeClaude())

        assert seen == {"before": False, "after": False}
        assert order.intent_check.verdict == Verdict.unsure


def test_all_order_responses_show_the_check_result(client, paypal):
    order = create_vault_order(client, paypal)
    decision_id = order["intent_check"]["id"]
    order_url = "/orders/" + str(order["id"])

    detail = client.get(order_url).json()
    assert detail["intent_check"]["id"] == decision_id
    assert detail["decisions"][0]["id"] == decision_id
    assert detail["decisions"][0]["mismatched_fields"] == []
    assert len(detail["decisions"][0]["rule_results"]) == 4

    assert client.get("/orders").json()[0]["intent_check"]["id"] == decision_id

    mock_capture(paypal)
    approve(client, order)
    assert client.get(order_url).json()["intent_check"]["id"] == decision_id


def test_intent_check_is_the_newest_decision(client, paypal, db_sessions):
    order = create_vault_order(client, paypal)
    with db_sessions() as session:
        session.add(Decision(
            order_id=order["id"],
            verdict=Verdict.unsure,
            reason="Second look.",
            checked_by=CheckedBy.ai,
        ))
        session.commit()

    detail = client.get("/orders/" + str(order["id"])).json()

    assert detail["intent_check"]["reason"] == "Second look."
    assert detail["decisions"][-1]["reason"] == "Second look."


def test_decision_from_before_the_check_was_wired_in_still_reads(client, paypal, db_sessions):
    # rows saved earlier have null in the new columns
    order = create_unapproved_order(client, paypal)
    with db_sessions() as session:
        session.add(Decision(
            order_id=order["id"],
            verdict=Verdict.match,
            reason="Old row.",
            checked_by=CheckedBy.rules,
        ))
        session.commit()

    check = client.get("/orders/" + str(order["id"])).json()["intent_check"]

    assert check["reason"] == "Old row."
    assert check["mismatched_fields"] is None
    assert check["rule_results"] is None
    assert check["latency_ms"] is None


# ---- auto-decide ----


def test_clean_match_auto_captures(client, paypal, claude):
    claude.answer = match_answer(risk_score=5)
    mock_capture(paypal)

    order = create_vault_order(client, paypal)

    assert order["status"] == "captured"
    assert order["review_state"] == "auto_approved"
    assert order["capture_id"] == "2GG279541U471931P"
    assert order["intent_check"]["verdict"] == "match"
    # no human was involved
    assert order["approval"] is None
    assert len(paypal_calls_to(paypal, "/capture")) == 1


def test_clean_match_auto_captures_on_the_authorize_path_too(client, paypal, claude):
    claude.answer = match_answer(risk_score=5)
    order = create_unapproved_order(client, paypal)
    # nothing to capture yet, the buyer hasn't approved
    assert order["status"] == "created"
    assert order["review_state"] is None

    paypal.add(
        responses.POST,
        BASE + "/v2/checkout/orders/8XU44448NV0000000/authorize",
        status=201,
        json=authorized_paypal_order("8XU44448NV0000000", "7PS21555RT234531W"),
    )
    mock_capture(paypal, authorization_id="7PS21555RT234531W")

    authorized = client.post("/orders/" + str(order["id"]) + "/authorize")

    assert authorized.status_code == 200
    assert authorized.json()["status"] == "captured"
    assert authorized.json()["review_state"] == "auto_approved"


@pytest.mark.parametrize("risk_score", [30, 31, 75, 100])
def test_high_risk_match_goes_to_review(client, paypal, claude, risk_score):
    # 30 itself goes to a human: it has to be below the limit to capture alone
    claude.answer = match_answer(risk_score=risk_score)

    order = create_vault_order(client, paypal)

    assert order["status"] == "authorized"
    assert order["review_state"] == "needs_review"
    assert order["capture_id"] is None
    assert paypal_calls_to(paypal, "/capture") == []


def test_just_under_the_risk_limit_auto_captures(client, paypal, claude):
    claude.answer = match_answer(risk_score=29)
    mock_capture(paypal)

    assert create_vault_order(client, paypal)["review_state"] == "auto_approved"


def test_risk_limit_comes_from_config(client, paypal, claude, monkeypatch):
    monkeypatch.setenv("AUTO_CAPTURE_MAX_RISK", "10")
    get_settings.cache_clear()
    try:
        claude.answer = match_answer(risk_score=15)

        order = create_vault_order(client, paypal)
    finally:
        monkeypatch.delenv("AUTO_CAPTURE_MAX_RISK")
        get_settings.cache_clear()

    assert order["review_state"] == "needs_review"
    assert paypal_calls_to(paypal, "/capture") == []


@pytest.mark.parametrize("verdict", ["mismatch", "unsure"])
def test_anything_but_a_match_goes_to_review(client, paypal, claude, verdict):
    # risk 0 on purpose: the verdict alone has to be enough to stop it
    claude.answer = {
        "verdict": verdict,
        "risk_score": 0,
        "reason": "Doesn't look right.",
        "mismatched_fields": [],
    }

    order = create_vault_order(client, paypal)

    assert order["status"] == "authorized"
    assert order["review_state"] == "needs_review"
    assert paypal_calls_to(paypal, "/capture") == []


def test_hard_rule_failure_goes_to_review_without_moving_money(client, paypal):
    response = post_vault_order(client, paypal, mandate_id=2)

    assert response.json()["review_state"] == "needs_review"
    assert paypal_calls_to(paypal, "/capture") == []


def test_claude_being_down_goes_to_review(client, paypal, claude):
    claude.error = RuntimeError("Claude is having a day")

    order = create_vault_order(client, paypal)

    assert order["review_state"] == "needs_review"
    assert paypal_calls_to(paypal, "/capture") == []


def test_null_intent_check_goes_to_review(client, paypal, claude, monkeypatch):
    # Claude said match but the result never got saved, and an unrecorded yes can't move money
    claude.answer = match_answer(risk_score=5)

    def broken_build(order, result):
        raise SQLAlchemyError("database went away")

    monkeypatch.setattr(orders, "build_decision", broken_build)

    order = create_vault_order(client, paypal)

    assert order["intent_check"] is None
    assert order["status"] == "authorized"
    assert order["review_state"] == "needs_review"
    assert paypal_calls_to(paypal, "/capture") == []


def test_auto_capture_paypal_failure_goes_to_review(client, paypal, claude, db_sessions, caplog):
    claude.answer = match_answer(risk_score=5)
    paypal.add(
        responses.POST,
        BASE + "/v2/payments/authorizations/0AW2184448108334S/capture",
        status=422,
        json={"name": "UNPROCESSABLE_ENTITY", "debug_id": "abc123",
              "details": [{"issue": "AUTHORIZATION_EXPIRED"}]},
    )

    with caplog.at_level(logging.ERROR):
        order = create_vault_order(client, paypal)

    # the order itself is fine, it just needs a human now, so the agent doesn't see an error
    assert order["status"] == "authorized"
    assert order["review_state"] == "needs_review"
    assert order["capture_id"] is None
    assert "Auto-capture failed for order " + str(order["id"]) in caplog.text
    # exactly one try inside the request
    assert len(paypal_calls_to(paypal, "/capture")) == 1
    # and the claim is gone, so the human can act right away
    assert saved_order(db_sessions, order["id"]).payment_claimed_at is None


def test_auto_capture_network_failure_goes_to_review(client, paypal, claude):
    # a timeout isn't a PayPalError, and it's the likeliest failure of all
    claude.answer = match_answer(risk_score=5)
    paypal.add(
        responses.POST,
        BASE + "/v2/payments/authorizations/0AW2184448108334S/capture",
        body=requests.exceptions.ConnectTimeout("PayPal took too long"),
    )

    order = create_vault_order(client, paypal)

    assert order["status"] == "authorized"
    assert order["review_state"] == "needs_review"


def test_human_can_approve_after_auto_capture_failed(client, paypal, claude):
    claude.answer = match_answer(risk_score=5)
    paypal.add(
        responses.POST,
        BASE + "/v2/payments/authorizations/0AW2184448108334S/capture",
        status=500,
        json={"name": "INTERNAL_SERVER_ERROR"},
    )
    order = create_vault_order(client, paypal)
    paypal.replace(
        responses.POST,
        BASE + "/v2/payments/authorizations/0AW2184448108334S/capture",
        status=201,
        json={"id": "2GG279541U471931P", "status": "COMPLETED"},
    )

    response = approve(client, order)

    assert response.status_code == 200
    assert response.json()["review_state"] == "approved"
    # the same request id both times: if that 500 really went through, this gets it back
    calls = paypal_calls_to(paypal, "/capture")
    assert len(calls) == 2
    assert (calls[0].request.headers["PayPal-Request-Id"]
            == calls[1].request.headers["PayPal-Request-Id"])


def test_declined_auto_capture_is_failed_and_left_in_review(client, paypal, claude, caplog):
    claude.answer = match_answer(risk_score=5)
    paypal.add(
        responses.POST,
        BASE + "/v2/payments/authorizations/0AW2184448108334S/capture",
        status=201,
        json={"id": "2GG279541U471931P", "status": "DECLINED"},
    )

    with caplog.at_level(logging.ERROR):
        order = create_vault_order(client, paypal)

    assert order["status"] == "failed"
    assert order["review_state"] == "needs_review"
    assert "declined the auto-capture" in caplog.text


def test_human_approving_during_the_check_stops_the_auto_capture(client, paypal, claude,
                                                                 db_sessions):
    # the order is visible and waiting for the seconds Claude takes. if a human settles it in
    # that gap, the auto-capture has to see that and back off, not trust what it loaded earlier
    real_create = claude.beta.messages.create

    def human_rejects_during_the_check(**kwargs):
        with db_sessions() as session:
            order = session.scalars(select(Order)).one()
            order.status = OrderStatus.voided
            order.review_state = ReviewState.rejected
            session.commit()
        return real_create(**kwargs)

    claude.beta.messages.create = human_rejects_during_the_check
    claude.answer = match_answer(risk_score=5)

    order = create_vault_order(client, paypal)

    assert order["status"] == "voided"
    assert order["review_state"] == "rejected"
    assert paypal_calls_to(paypal, "/capture") == []


# ---- approve and reject ----


def test_approve_captures_and_records_the_tap(client, paypal, db_sessions):
    order = create_vault_order(client, paypal)
    mock_capture(paypal)

    response = approve(client, order, decision_ms=4200)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "captured"
    assert body["review_state"] == "approved"

    with db_sessions() as session:
        saved = session.get(Order, order["id"])
        assert saved.status == OrderStatus.captured
        assert saved.review_state == ReviewState.approved
        assert saved.capture_id == "2GG279541U471931P"
        assert saved.payment_claimed_at is None
        assert len(saved.approvals) == 1
        approval = saved.approvals[0]
        assert approval.action == ApprovalAction.approve
        assert approval.client_decision_ms == 4200

    merchant_view = client.get("/orders/" + str(order["id"])).json()
    assert merchant_view["capture_id"] == "2GG279541U471931P"
    assert merchant_view["approval"]["action"] == "approve"


def test_reject_voids_and_records_the_tap(client, paypal, db_sessions):
    order = create_vault_order(client, paypal)
    paypal.add(
        responses.POST,
        BASE + "/v2/payments/authorizations/0AW2184448108334S/void",
        status=200,
        json={"id": "0AW2184448108334S", "status": "VOIDED"},
    )

    response = reject(client, order, decision_ms=900)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "voided"
    assert body["review_state"] == "rejected"

    with db_sessions() as session:
        saved = session.get(Order, order["id"])
        assert saved.void_result == "VOIDED"
        assert saved.capture_id is None
        assert saved.payment_claimed_at is None
        assert len(saved.approvals) == 1
        assert saved.approvals[0].action == ApprovalAction.reject
        assert saved.approvals[0].client_decision_ms == 900


def test_reject_handles_paypals_empty_204(client, paypal):
    # both are documented successes, it depends on the Prefer header
    order = create_vault_order(client, paypal)
    mock_void(paypal)

    assert reject(client, order).status_code == 200
    assert client.get("/orders/" + str(order["id"])).json()["void_result"] == "VOIDED"


def test_double_approve_is_a_409(client, paypal):
    # this one is taps in a row. tests/test_orders_postgres.py does taps at the same moment
    order = create_vault_order(client, paypal)
    mock_capture(paypal)
    assert approve(client, order).status_code == 200

    second = approve(client, order)

    assert second.status_code == 409
    assert second.json()["detail"]["status"] == "captured"
    assert second.json()["detail"]["review_state"] == "approved"
    assert len(paypal_calls_to(paypal, "/capture")) == 1


def test_approve_after_reject_is_a_409(client, paypal):
    order = create_vault_order(client, paypal)
    mock_void(paypal)
    assert reject(client, order).status_code == 200

    second = approve(client, order)

    assert second.status_code == 409
    assert second.json()["detail"]["status"] == "voided"
    assert second.json()["detail"]["review_state"] == "rejected"
    assert paypal_calls_to(paypal, "/capture") == []


def test_reject_after_approve_is_a_409(client, paypal):
    order = create_vault_order(client, paypal)
    mock_capture(paypal)
    assert approve(client, order).status_code == 200

    second = reject(client, order)

    assert second.status_code == 409
    assert second.json()["detail"]["review_state"] == "approved"
    assert paypal_calls_to(paypal, "/void") == []


def test_cant_review_an_auto_approved_order(client, paypal, claude):
    claude.answer = match_answer(risk_score=5)
    mock_capture(paypal)
    order = create_vault_order(client, paypal)

    for response in [approve(client, order), reject(client, order)]:
        assert response.status_code == 409
        assert response.json()["detail"]["status"] == "captured"
        assert response.json()["detail"]["review_state"] == "auto_approved"


def test_cant_review_before_authorized(client, paypal):
    order = create_unapproved_order(client, paypal)

    for response in [approve(client, order), reject(client, order)]:
        assert response.status_code == 409
        assert response.json()["detail"]["status"] == "created"
        assert response.json()["detail"]["review_state"] is None
    # and PayPal was never asked to capture or void anything
    assert paypal_calls_to(paypal, "/capture") == []
    assert paypal_calls_to(paypal, "/void") == []


def test_cant_review_a_failed_order(client, paypal):
    order = post_vault_order(client, paypal, status="DENIED").json()

    assert approve(client, order).status_code == 409
    assert paypal_calls_to(paypal, "/capture") == []


def test_review_is_blocked_while_a_payment_is_in_flight(client, paypal, db_sessions):
    # what a second tap sees while the first one is waiting on PayPal
    order = create_vault_order(client, paypal)
    with db_sessions() as session:
        saved = session.get(Order, order["id"])
        saved.payment_claimed_at = datetime.now(timezone.utc)
        session.commit()

    response = approve(client, order)

    assert response.status_code == 409
    assert "payment in progress" in response.json()["detail"]["message"]
    assert response.json()["detail"]["status"] == "authorized"
    assert response.json()["detail"]["review_state"] == "needs_review"
    assert paypal_calls_to(paypal, "/capture") == []


def test_abandoned_claim_expires(client, paypal, db_sessions):
    # a request that crashed mid-capture can't leave the order stuck forever
    order = create_vault_order(client, paypal)
    with db_sessions() as session:
        saved = session.get(Order, order["id"])
        saved.payment_claimed_at = datetime.now(timezone.utc) - timedelta(seconds=121)
        session.commit()
    mock_capture(paypal)

    assert approve(client, order).status_code == 200


@pytest.mark.parametrize("body", [{"decision_ms": -1}, {"decision_ms": "soon"},
                                  {"decision_ms": 2147483648}])
def test_client_decision_ms_has_to_be_sensible_if_sent(client, paypal, db_sessions, body):
    order = create_vault_order(client, paypal)
    token = mint_link(client, order).json()["review_token"]

    assert client.post("/review/" + token + "/approve", json=body).status_code == 422
    assert client.post("/review/" + token + "/reject", json=body).status_code == 422
    assert paypal_calls_to(paypal, "/capture") == []
    assert paypal_calls_to(paypal, "/void") == []
    # a bad request doesn't use up the link
    with db_sessions() as session:
        assert session.get(Order, order["id"]).review_token_used_at is None


def test_old_routes_are_gone(client, paypal):
    # any of these would let an agent settle its own flagged order
    order = create_vault_order(client, paypal)
    url = "/orders/" + str(order["id"])

    for name in ["/capture", "/void", "/approve", "/reject"]:
        assert client.post(url + name, json={"decision_ms": 1}).status_code == 404
    assert paypal_calls_to(paypal, "/capture") == []
    assert paypal_calls_to(paypal, "/void") == []


# ---- PayPal failing on approve and reject ----


@pytest.mark.parametrize("failure", [
    {"status": 422, "json": {"name": "UNPROCESSABLE_ENTITY", "debug_id": "90bff07028f7f",
                             "details": [{"issue": "AUTHORIZATION_EXPIRED"}]}},
    {"status": 500, "json": {"name": "INTERNAL_SERVER_ERROR", "debug_id": "90bff07028f7f"}},
    {"body": requests.exceptions.ReadTimeout("PayPal took too long")},
])
def test_paypal_failure_on_approve_keeps_needs_review(client, paypal, db_sessions, caplog, failure):
    order = create_vault_order(client, paypal)
    paypal.add(
        responses.POST,
        BASE + "/v2/payments/authorizations/0AW2184448108334S/capture",
        **failure,
    )

    with caplog.at_level(logging.ERROR):
        response = approve(client, order)

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["message"] == "PayPal capture failed, the order is still waiting for review"
    assert detail["status"] == "authorized"
    assert detail["review_state"] == "needs_review"
    assert "PayPal capture failed for order " + str(order["id"]) in caplog.text

    with db_sessions() as session:
        saved = session.get(Order, order["id"])
        assert saved.status == OrderStatus.authorized
        assert saved.review_state == ReviewState.needs_review
        assert saved.capture_id is None
        # a tap that didn't do anything isn't a decision
        assert saved.approvals == []
        assert saved.payment_claimed_at is None


def test_approve_can_be_retried_after_a_paypal_failure(client, paypal):
    order = create_vault_order(client, paypal)
    paypal.add(
        responses.POST,
        BASE + "/v2/payments/authorizations/0AW2184448108334S/capture",
        status=500,
        json={"name": "INTERNAL_SERVER_ERROR"},
    )
    assert approve(client, order).status_code == 502
    paypal.replace(
        responses.POST,
        BASE + "/v2/payments/authorizations/0AW2184448108334S/capture",
        status=201,
        json={"id": "2GG279541U471931P", "status": "COMPLETED"},
    )

    assert approve(client, order).json()["review_state"] == "approved"


def test_paypal_failure_on_reject_keeps_needs_review(client, paypal, db_sessions, caplog):
    order = create_vault_order(client, paypal)
    paypal.add(
        responses.POST,
        BASE + "/v2/payments/authorizations/0AW2184448108334S/void",
        status=500,
        json={"name": "INTERNAL_SERVER_ERROR", "debug_id": "abc"},
    )

    with caplog.at_level(logging.ERROR):
        response = reject(client, order)

    assert response.status_code == 502
    assert response.json()["detail"]["review_state"] == "needs_review"
    assert "PayPal void failed for order " + str(order["id"]) in caplog.text
    with db_sessions() as session:
        saved = session.get(Order, order["id"])
        assert saved.review_state == ReviewState.needs_review
        assert saved.void_result is None
        assert saved.approvals == []


def test_declined_capture_on_approve_is_a_502_and_not_a_decision(client, paypal, db_sessions):
    order = create_vault_order(client, paypal)
    paypal.add(
        responses.POST,
        BASE + "/v2/payments/authorizations/0AW2184448108334S/capture",
        status=201,
        json={"id": "2GG279541U471931P", "status": "DECLINED"},
    )

    response = approve(client, order)

    assert response.status_code == 502
    assert response.json()["detail"]["status"] == "failed"
    with db_sessions() as session:
        saved = session.get(Order, order["id"])
        assert saved.status == OrderStatus.failed
        assert saved.review_state == ReviewState.needs_review
        assert saved.approvals == []


def test_capture_saved_failing_after_paypal_succeeded_is_a_502(client, paypal, caplog, monkeypatch):
    # the worst spot: money moved and we couldn't write it down. the capture id has to be in the
    # log, and the claim stays on so nobody voids it before anyone has looked
    order = create_vault_order(client, paypal)
    mock_capture(paypal)

    def broken_finish(*args):
        raise SQLAlchemyError("database went away")

    monkeypatch.setattr(orders, "finish_capture", broken_finish)

    with caplog.at_level(logging.ERROR):
        response = approve(client, order)

    assert response.status_code == 502
    assert "went through" in response.json()["detail"]["message"]
    assert "2GG279541U471931P" in caplog.text
    second = reject(client, order)
    assert second.status_code == 409
    assert "payment in progress" in second.json()["detail"]["message"]


# ---- PayPal-Request-Id ----


def test_capture_and_void_send_derived_request_ids(client, paypal):
    capture_order = create_vault_order(client, paypal, "ORDER1", "AUTH1")
    void_order = create_vault_order(client, paypal, "ORDER2", "AUTH2")
    mock_capture(paypal, authorization_id="AUTH1")
    mock_void(paypal, authorization_id="AUTH2")

    approve(client, capture_order)
    reject(client, void_order)

    capture_header = paypal_calls_to(paypal, "/capture")[0].request.headers["PayPal-Request-Id"]
    void_header = paypal_calls_to(paypal, "/void")[0].request.headers["PayPal-Request-Id"]
    assert capture_header == orders.payment_request_id(
        SimpleNamespace(id=capture_order["id"], authorization_id="AUTH1"), "capture"
    )
    assert void_header == orders.payment_request_id(
        SimpleNamespace(id=void_order["id"], authorization_id="AUTH2"), "void"
    )


def test_request_id_changes_with_each_part_and_nothing_else():
    order = SimpleNamespace(id=1, authorization_id="AUTH1")

    first = orders.payment_request_id(order, "capture")

    assert first == orders.payment_request_id(order, "capture")
    assert first != orders.payment_request_id(order, "void")
    assert first != orders.payment_request_id(
        SimpleNamespace(id=2, authorization_id="AUTH1"), "capture"
    )
    # order 1 in another database, against the same sandbox account
    assert first != orders.payment_request_id(
        SimpleNamespace(id=1, authorization_id="AUTH2"), "capture"
    )
    # still a real UUID, like every other request id we send
    assert str(uuid.UUID(first)) == first


def test_auto_capture_and_approve_share_a_request_id(client, paypal, claude):
    # if an auto-capture really went through but we never heard back, a human approving afterwards
    # has to get that capture back, not make a second one
    claude.answer = match_answer(risk_score=5)
    paypal.add(
        responses.POST,
        BASE + "/v2/payments/authorizations/0AW2184448108334S/capture",
        body=requests.exceptions.ReadTimeout("no answer"),
    )
    order = create_vault_order(client, paypal)
    paypal.replace(
        responses.POST,
        BASE + "/v2/payments/authorizations/0AW2184448108334S/capture",
        status=200,
        json={"id": "2GG279541U471931P", "status": "COMPLETED"},
    )

    approve(client, order)

    calls = paypal_calls_to(paypal, "/capture")
    assert (calls[0].request.headers["PayPal-Request-Id"]
            == calls[1].request.headers["PayPal-Request-Id"])


# ---- what the API returns ----


def test_every_order_response_has_the_same_shape(client, paypal):
    # docs/api.md promises these names to the frontend
    order = create_vault_order(client, paypal)
    mock_capture(paypal)
    approve(client, order)
    listed = client.get("/orders").json()[0]
    detail = client.get("/orders/" + str(order["id"])).json()

    shared = {
        "id", "mandate_id", "paypal_order_id", "authorization_id", "capture_id", "void_result",
        "vault_id", "merchant", "items", "total", "shipping_amount", "currency", "status",
        "review_state", "authorization_expires_at", "watch_mode", "created_at", "mandate",
        "intent_check", "approval",
    }
    assert set(listed) == shared
    assert set(detail) == shared | {"decisions", "approvals"}

    assert set(listed["mandate"]) == {
        "id", "buyer_label", "raw_request", "rules", "max_price", "max_price_inclusive",
        "merchant", "expires_at", "created_at",
    }
    assert set(listed["intent_check"]) == {
        "id", "verdict", "risk_score", "reason", "mismatched_fields", "rule_results",
        "latency_ms", "checked_by", "created_at",
    }
    assert set(listed["approval"]) == {
        "id", "action", "decided_by", "opened_at", "decided_at", "decision_ms",
        "client_decision_ms",
    }
    # the claim and the token are plumbing, not part of the contract
    assert "payment_claimed_at" not in listed
    assert "review_token_hash" not in listed
    assert "review_token_used_at" not in listed


def test_list_includes_mandate_check_and_approval(client, paypal):
    order = create_vault_order(client, paypal)
    mock_capture(paypal)
    approve(client, order)

    listed = client.get("/orders").json()[0]

    assert listed["mandate"]["merchant"] == "Fernwood Supply Co"
    assert listed["intent_check"]["verdict"] == "unsure"
    assert listed["approval"]["action"] == "approve"
    assert listed["review_state"] == "approved"


def test_order_with_no_approval_says_so(client, paypal):
    order = create_vault_order(client, paypal)

    assert client.get("/orders").json()[0]["approval"] is None
    assert client.get("/orders/" + str(order["id"])).json()["approval"] is None


def test_list_doesnt_run_one_query_per_order(client, paypal, db_sessions):
    # with lazy loading this would grow with every order, on the page the dashboard polls
    for number in range(3):
        create_vault_order(client, paypal, "ORDER" + str(number), "AUTH" + str(number))
    engine = db_sessions.kw["bind"]
    statements = []

    def count_statement(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", count_statement)
    try:
        response = client.get("/orders")
    finally:
        event.remove(engine, "before_cursor_execute", count_statement)

    assert len(response.json()) == 3
    # the orders, then one each for mandates, decisions and approvals
    assert len(statements) == 4


# ---- API keys ----


@pytest.mark.parametrize(("method", "path"), [
    ("GET", "/orders"),
    ("GET", "/orders/1"),
    ("POST", "/orders/1/review-link"),
])
def test_agent_key_gets_401_on_merchant_routes(no_key_client, method, path):
    response = no_key_client.request(method, path, headers=AGENT_HEADERS)

    assert response.status_code == 401


def test_agent_key_cant_approve_its_own_order(client, no_key_client, paypal, db_sessions):
    # the whole point. there are three ways it could try, and none of them moves any money
    order = create_vault_order(client, paypal)
    assert order["review_state"] == "needs_review"
    order_url = "/orders/" + str(order["id"])

    # 1. get itself a review link: 401
    assert no_key_client.post(order_url + "/review-link", headers=AGENT_HEADERS).status_code == 401

    # 2. the old approve route, which no longer exists
    response = no_key_client.post(
        order_url + "/approve", json={"decision_ms": 1}, headers=AGENT_HEADERS
    )
    assert response.status_code == 404

    # 3. the new one, without a real token. its key doesn't stand in for one
    response = no_key_client.post(
        "/review/made-up-token/approve", json={"decision_ms": 1}, headers=AGENT_HEADERS
    )
    assert response.status_code == 404

    assert paypal_calls_to(paypal, "/capture") == []
    assert saved_order(db_sessions, order["id"]).review_state == ReviewState.needs_review


@pytest.mark.parametrize("path", ["/orders", "/orders/1/authorize"])
def test_merchant_key_cant_place_orders(no_key_client, paypal, path):
    response = no_key_client.post(path, json=ORDER_BODY, headers=MERCHANT_HEADERS)

    assert response.status_code == 401
    assert len(paypal.calls) == 0


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": ""}, {"X-API-Key": "wrong"}])
@pytest.mark.parametrize(("method", "path"), [
    ("GET", "/orders"),
    ("POST", "/orders"),
    ("POST", "/orders/1/authorize"),
    ("POST", "/orders/1/review-link"),
])
def test_missing_or_wrong_key_is_401(no_key_client, paypal, method, path, headers):
    # an invalid body too: a stranger gets a 401, not a description of what the API expects
    response = no_key_client.request(method, path, headers=headers, json={"nope": True})

    assert response.status_code == 401
    assert len(paypal.calls) == 0


def test_unset_keys_lock_everything(no_key_client, paypal, monkeypatch):
    # a forgotten env var has to fail closed, an empty header can't match an empty setting
    monkeypatch.setenv("AGENT_API_KEY", "")
    monkeypatch.setenv("MERCHANT_API_KEY", "")
    get_settings.cache_clear()

    for headers in [{}, {"X-API-Key": ""}, AGENT_HEADERS, MERCHANT_HEADERS]:
        assert no_key_client.get("/orders", headers=headers).status_code == 401
        assert no_key_client.post("/orders", json=ORDER_BODY, headers=headers).status_code == 401


def test_every_route_in_the_app_needs_a_key():
    # so a route added later can't quietly be left open. skipped on purpose: /health is for Render,
    # /auth/session is how you log in, and /review/ is authorized by the token in its path
    from backend.app.main import app

    no_key = TestClient(app)
    checked = 0
    # the OpenAPI doc lists every served path, however the routers are nested
    for route_path, operations in app.openapi()["paths"].items():
        if route_path in ("/health", "/auth/session") or route_path.startswith("/review/"):
            continue
        path = route_path.replace("{order_id}", "1").replace("{mandate_id}", "1")
        path = path.replace("{dispute_id}", "1")
        for method in operations:
            assert no_key.request(method, path).status_code == 401, method + " " + route_path
            checked += 1
    # three for mandates, four for orders, one for review links, one for agent status, two for the
    # demo routes, one for stats, five for disputes. if this drops to 0 the loop above isn't
    # checking anything, so it can't just pass
    assert checked == 17


# ---- session tokens on the real routes ----


def session_headers():
    return {"Authorization": "Bearer " + issue_session_token()[0]}


def test_dashboard_session_works_on_every_merchant_route(client, no_key_client, paypal):
    order = create_vault_order(client, paypal)
    url = "/orders/" + str(order["id"])

    assert no_key_client.get("/orders", headers=session_headers()).status_code == 200
    assert no_key_client.get(url, headers=session_headers()).status_code == 200
    link = no_key_client.post(url + "/review-link", headers=session_headers())
    assert link.status_code == 201
    assert link.json()["review_token"]


def test_session_token_cant_do_what_the_agent_does(no_key_client, paypal):
    # a dashboard session reads and creates links. it doesn't place orders
    response = no_key_client.post("/orders", json=ORDER_BODY, headers=session_headers())

    assert response.status_code == 401
    assert no_key_client.get("/agent/orders/1", headers=session_headers()).status_code == 401
    assert no_key_client.post("/orders/1/authorize", headers=session_headers()).status_code == 401
    assert len(paypal.calls) == 0


def test_agent_key_as_a_bearer_token_gets_nothing(no_key_client):
    headers = {"Authorization": "Bearer " + AGENT_HEADERS["X-API-Key"]}

    assert no_key_client.get("/orders", headers=headers).status_code == 401


# ---- what the agent can see of an order ----


AGENT_FIELDS = {"id", "status", "review_state", "total"}


def test_agent_gets_only_four_fields_from_a_vault_order(client, paypal, claude):
    claude.answer = {
        "verdict": "mismatch",
        "risk_score": 90,
        "reason": "Ordered red, mandate says black",
        "mismatched_fields": ["color"],
    }
    response = post_vault_order(client, paypal)

    assert response.status_code == 201
    assert set(response.json()) == AGENT_FIELDS
    assert response.json()["status"] == "authorized"
    assert response.json()["review_state"] == "needs_review"
    assert response.json()["total"] == "66.00"
    assert "Ordered red" not in response.text
    assert "intent_check" not in response.text
    assert "Trail runners and socks" not in response.text


def test_agent_gets_only_four_fields_from_authorize(client, paypal, claude):
    claude.answer = {
        "verdict": "mismatch",
        "risk_score": 90,
        "reason": "Ordered red, mandate says black",
        "mismatched_fields": ["color"],
    }
    order = create_unapproved_order(client, paypal)
    paypal.add(
        responses.POST,
        BASE + "/v2/checkout/orders/8XU44448NV0000000/authorize",
        status=201,
        json=authorized_paypal_order("8XU44448NV0000000", "7PS21555RT234531W"),
    )

    response = client.post("/orders/" + str(order["id"]) + "/authorize")

    assert response.status_code == 200
    assert set(response.json()) == AGENT_FIELDS
    assert response.json()["review_state"] == "needs_review"
    assert "Ordered red" not in response.text
    assert "7PS21555RT234531W" not in response.text


def test_agent_gets_four_fields_from_an_auto_captured_order_too(client, paypal, claude):
    claude.answer = match_answer(risk_score=5)
    mock_capture(paypal)

    response = post_vault_order(client, paypal)

    assert set(response.json()) == AGENT_FIELDS
    assert response.json()["status"] == "captured"
    assert response.json()["review_state"] == "auto_approved"
    assert "2GG279541U471931P" not in response.text


def test_no_vault_order_also_gets_the_link_the_buyer_needs(client, paypal):
    # the one exception: without it, nobody could ever approve the order at PayPal
    paypal.add(
        responses.POST, BASE + "/v2/checkout/orders", status=201,
        json=payer_action_paypal_order("8XU44448NV0000000"),
    )

    response = client.post("/orders", json=ORDER_BODY)

    assert response.status_code == 201
    assert set(response.json()) == AGENT_FIELDS | {"payer_action_url"}
    assert response.json()["payer_action_url"].endswith("token=8XU44448NV0000000")
    assert response.json()["status"] == "created"
    assert response.json()["review_state"] is None


def test_merchant_still_sees_what_the_agent_doesnt(client, paypal, claude):
    claude.answer = {
        "verdict": "mismatch",
        "risk_score": 90,
        "reason": "Ordered red, mandate says black",
        "mismatched_fields": ["color"],
    }
    placed = post_vault_order(client, paypal).json()

    detail = client.get("/orders/" + str(placed["id"])).json()

    assert detail["intent_check"]["reason"] == "Ordered red, mandate says black"
    assert detail["mandate"]["merchant"] == "Fernwood Supply Co"


def test_agent_status_is_exactly_four_fields(client, paypal):
    order = create_vault_order(client, paypal)

    response = client.get("/agent/orders/" + str(order["id"]))

    assert response.status_code == 200
    assert response.json() == {
        "id": order["id"],
        "status": "authorized",
        "review_state": "needs_review",
        "total": "66.00",
    }


def test_agent_status_follows_the_order(client, paypal):
    order = create_vault_order(client, paypal)
    url = "/agent/orders/" + str(order["id"])
    mock_capture(paypal)

    approve(client, order)

    assert client.get(url).json()["status"] == "captured"
    assert client.get(url).json()["review_state"] == "approved"


def test_agent_status_cant_leak_a_token_or_a_reason(client, paypal, claude):
    # with a live link on the order, and a reason and mandate worth leaking
    claude.answer = {
        "verdict": "mismatch",
        "risk_score": 90,
        "reason": "Ordered red, mandate says black",
        "mismatched_fields": ["color"],
    }
    order = create_vault_order(client, paypal)
    token = mint_link(client, order).json()["review_token"]
    hashed = hashlib.sha256(token.encode()).hexdigest()

    response = client.get("/agent/orders/" + str(order["id"]))

    assert_no_token_in(response, token, hashed)
    for secret in ["Ordered red", "Trail runners and socks", "Fernwood", "Sam", VAULT_ID,
                   order["authorization_id"], order["paypal_order_id"], "color"]:
        assert secret not in response.text
    # and the same after the link is used, when more of the row is filled in
    mock_capture(paypal)
    client.post("/review/" + token + "/approve", json={})
    used = client.get("/agent/orders/" + str(order["id"]))
    assert_no_token_in(used, token, hashed)
    assert "2GG279541U471931P" not in used.text
    assert set(used.json()) == {"id", "status", "review_state", "total"}


def test_agent_status_needs_the_agent_key(no_key_client, client, paypal):
    order = create_vault_order(client, paypal)
    url = "/agent/orders/" + str(order["id"])

    assert no_key_client.get(url).status_code == 401
    assert no_key_client.get(url, headers=MERCHANT_HEADERS).status_code == 401
    assert no_key_client.get(url, headers={"X-API-Key": "wrong"}).status_code == 401
    assert no_key_client.get(url, headers=AGENT_HEADERS).status_code == 200


def test_agent_status_for_a_missing_order_is_a_404(client):
    assert client.get("/agent/orders/999").status_code == 404


# ---- review links: where the token goes ----


def walk_keys(value):
    # every key anywhere in a JSON body
    found = []
    if isinstance(value, dict):
        for key, inner in value.items():
            found.append(key)
            found.extend(walk_keys(inner))
    elif isinstance(value, list):
        for inner in value:
            found.extend(walk_keys(inner))
    return found


def assert_no_token_in(response, *secrets_to_find):
    for secret in secrets_to_find:
        assert secret not in response.text
    for key in walk_keys(response.json()):
        assert "token" not in key.lower()


def test_agent_responses_never_contain_a_token(client, paypal, claude, db_sessions):
    # a token really exists when the agent asks, so this would catch it leaking
    first = create_vault_order(client, paypal, "ORDER1", "AUTH1")
    token = mint_link(client, first).json()["review_token"]
    hashed = hashlib.sha256(token.encode()).hexdigest()
    assert saved_order(db_sessions, first["id"]).review_token_hash == hashed

    # every response the agent key can get: another vault order
    paypal.add(
        responses.POST, BASE + "/v2/checkout/orders", status=201,
        json=authorized_paypal_order("ORDER2", "AUTH2"),
    )
    body = dict(ORDER_BODY)
    body["vault_id"] = VAULT_ID
    agent_responses = [client.post("/orders", json=body)]

    # a no-vault order, then authorizing it
    paypal.add(
        responses.POST, BASE + "/v2/checkout/orders", status=201,
        json=payer_action_paypal_order("ORDER3"),
    )
    unapproved = client.post("/orders", json=ORDER_BODY)
    agent_responses.append(unapproved)
    paypal.add(
        responses.POST, BASE + "/v2/checkout/orders/ORDER3/authorize", status=201,
        json=authorized_paypal_order("ORDER3", "AUTH3"),
    )
    agent_responses.append(client.post("/orders/" + str(unapproved.json()["id"]) + "/authorize"))

    # and the order that does have a live link, which only gets it a conflict
    agent_responses.append(client.post("/orders/" + str(first["id"]) + "/authorize"))

    assert [response.status_code for response in agent_responses] == [201, 201, 200, 409]
    for response in agent_responses:
        assert_no_token_in(response, token, hashed)


def test_agent_response_shapes_cant_hold_a_token():
    # the same check, on what the API is even able to send back
    for model in [orders.OrderOut, orders.OrderPlacedNeedsBuyer, orders.AgentOrderStatus]:
        for name in model.model_fields:
            assert "token" not in name.lower()


def test_token_only_ever_comes_back_when_its_created(client, paypal):
    # the merchant reads don't have it either: only the hash is stored, so they couldn't
    order = create_vault_order(client, paypal)
    link = mint_link(client, order)
    token = link.json()["review_token"]
    hashed = hashlib.sha256(token.encode()).hexdigest()

    assert link.status_code == 201
    assert set(link.json()) == {"review_token", "expires_at"}
    assert link.json()["expires_at"].startswith("2099-11-20T01:56:55")
    assert_no_token_in(client.get("/orders"), token, hashed)
    assert_no_token_in(client.get("/orders/" + str(order["id"])), token, hashed)
    assert_no_token_in(client.get("/review/" + token), token, hashed)


def test_only_the_hash_is_stored(client, paypal, db_sessions):
    order = create_vault_order(client, paypal)
    token = mint_link(client, order).json()["review_token"]

    with db_sessions() as session:
        row = session.execute(
            text("SELECT * FROM orders WHERE id = " + str(order["id"]))
        ).one()
    assert token not in str(tuple(row))
    assert hashlib.sha256(token.encode()).hexdigest() in str(tuple(row))
    # long enough that guessing one isn't a plan
    assert len(token) >= 43


def test_new_link_replaces_the_old_one(client, paypal):
    order = create_vault_order(client, paypal)
    old = mint_link(client, order).json()["review_token"]
    new = mint_link(client, order).json()["review_token"]

    assert new != old
    assert client.get("/review/" + old).status_code == 404
    assert client.get("/review/" + new).status_code == 200


def test_cant_create_a_link_for_an_order_with_nothing_to_review(client, paypal, claude):
    claude.answer = match_answer(risk_score=5)
    mock_capture(paypal)
    order = create_vault_order(client, paypal)

    response = mint_link(client, order)

    assert response.status_code == 409
    assert response.json()["detail"]["review_state"] == "auto_approved"


# ---- review links: using them ----


def test_review_page_has_what_confirmpage_needs_and_no_more(client, paypal, claude):
    claude.answer = {
        "verdict": "mismatch",
        "risk_score": 85,
        "reason": "Ordered red, mandate says black",
        "mismatched_fields": ["color"],
    }
    order = create_vault_order(client, paypal)
    token = mint_link(client, order).json()["review_token"]

    response = client.get("/review/" + token)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "id", "items", "total", "shipping_amount", "currency", "status", "review_state",
        "authorization_expires_at", "mandate", "intent_check",
    }
    assert body["items"][0]["name"] == "Trail runner"
    assert body["total"] == "66.00"
    assert body["mandate"]["raw_request"].startswith("Trail runners and socks")
    assert set(body["mandate"]) == {"buyer_label", "raw_request"}
    assert body["intent_check"]["reason"] == "Ordered red, mandate says black"
    assert body["status"] == "authorized"
    assert body["review_state"] == "needs_review"
    # a link holder doesn't get PayPal ids or the vault id
    assert order["authorization_id"] not in response.text
    assert VAULT_ID not in response.text


def test_unknown_token_is_a_404(client, paypal):
    assert client.get("/review/nope").status_code == 404
    assert client.post("/review/nope/opened").status_code == 404
    assert client.post("/review/nope/approve", json={}).status_code == 404
    assert client.post("/review/nope/reject", json={}).status_code == 404
    assert len(paypal.calls) == 0


@pytest.mark.parametrize("seconds_past", [0, 3600])
def test_expired_token_fails(client, paypal, db_sessions, seconds_past):
    order = create_vault_order(client, paypal)
    token = mint_link(client, order).json()["review_token"]
    set_on_order(
        db_sessions, order,
        authorization_expires_at=datetime.now(timezone.utc) - timedelta(seconds=seconds_past),
    )

    assert client.get("/review/" + token).status_code == 410
    assert client.post("/review/" + token + "/opened").status_code == 410
    approved = client.post("/review/" + token + "/approve", json={})
    rejected = client.post("/review/" + token + "/reject", json={})

    assert approved.status_code == 410
    assert "expired" in approved.json()["detail"]["message"]
    assert rejected.status_code == 410
    assert paypal_calls_to(paypal, "/capture") == []
    assert paypal_calls_to(paypal, "/void") == []
    saved = saved_order(db_sessions, order["id"])
    assert saved.review_state == ReviewState.needs_review
    assert saved.review_token_used_at is None
    # opening an expired link doesn't count as looking at the order
    with db_sessions() as session:
        assert session.query(Approval).count() == 0


def test_token_works_right_up_to_the_hold_expiring(client, paypal, db_sessions):
    order = create_vault_order(client, paypal)
    token = mint_link(client, order).json()["review_token"]
    set_on_order(
        db_sessions, order,
        authorization_expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
    )

    assert client.get("/review/" + token).status_code == 200


def test_no_link_for_a_hold_thats_already_expired(client, paypal, db_sessions):
    order = create_vault_order(client, paypal)
    set_on_order(
        db_sessions, order,
        authorization_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )

    response = mint_link(client, order)

    assert response.status_code == 409
    assert "expired" in response.json()["detail"]["message"]


def test_hold_with_no_expiry_on_record_counts_as_expired(client, paypal, db_sessions):
    order = create_vault_order(client, paypal)
    token = mint_link(client, order).json()["review_token"]
    set_on_order(db_sessions, order, authorization_expires_at=None)

    assert client.post("/review/" + token + "/approve", json={}).status_code == 410


def test_reused_token_fails(client, paypal):
    order = create_vault_order(client, paypal)
    token = mint_link(client, order).json()["review_token"]
    mock_capture(paypal)
    assert client.post("/review/" + token + "/approve", json={}).status_code == 200

    again = client.post("/review/" + token + "/approve", json={})
    other_way = client.post("/review/" + token + "/reject", json={})

    assert again.status_code == 409
    assert again.json()["detail"]["status"] == "captured"
    assert again.json()["detail"]["review_state"] == "approved"
    assert other_way.status_code == 409
    assert len(paypal_calls_to(paypal, "/capture")) == 1
    assert paypal_calls_to(paypal, "/void") == []
    # the page itself is gone too
    assert client.get("/review/" + token).status_code == 410
    assert client.post("/review/" + token + "/opened").status_code == 410


def test_token_is_used_up_in_the_same_commit_as_the_claim(client, paypal, db_sessions):
    # looked at from outside, at the moment PayPal gets called
    order = create_vault_order(client, paypal)
    token = mint_link(client, order).json()["review_token"]
    seen = {}

    def capture_callback(request):
        saved = saved_order(db_sessions, order["id"])
        seen["used"] = saved.review_token_used_at
        seen["claimed"] = saved.payment_claimed_at
        return (201, {}, json.dumps({"id": "2GG279541U471931P", "status": "COMPLETED"}))

    paypal.add_callback(
        responses.POST,
        BASE + "/v2/payments/authorizations/0AW2184448108334S/capture",
        callback=capture_callback,
    )

    assert client.post("/review/" + token + "/approve", json={}).status_code == 200

    assert seen["used"] is not None
    assert seen["used"] == seen["claimed"]


def test_paypal_failure_gives_the_link_back_for_a_retry(client, paypal, db_sessions):
    # used up with the claim, but PayPal definitely said no, so the tap didn't do anything
    order = create_vault_order(client, paypal)
    token = mint_link(client, order).json()["review_token"]
    paypal.add(
        responses.POST,
        BASE + "/v2/payments/authorizations/0AW2184448108334S/capture",
        status=500,
        json={"name": "INTERNAL_SERVER_ERROR"},
    )

    assert client.post("/review/" + token + "/approve", json={}).status_code == 502
    assert saved_order(db_sessions, order["id"]).review_token_used_at is None

    paypal.replace(
        responses.POST,
        BASE + "/v2/payments/authorizations/0AW2184448108334S/capture",
        status=201,
        json={"id": "2GG279541U471931P", "status": "COMPLETED"},
    )
    assert client.post("/review/" + token + "/approve", json={}).status_code == 200


def test_new_link_recovers_from_a_crash_mid_payment(client, paypal, db_sessions):
    # the request died after using up the link, so nothing ever gave it back
    order = create_vault_order(client, paypal)
    old = mint_link(client, order).json()["review_token"]
    long_ago = datetime.now(timezone.utc) - timedelta(seconds=121)
    set_on_order(db_sessions, order, review_token_used_at=long_ago, payment_claimed_at=long_ago)

    response = client.post("/review/" + old + "/approve", json={})
    assert response.status_code == 409
    assert "already been used" in response.json()["detail"]["message"]

    new = mint_link(client, order).json()["review_token"]
    mock_capture(paypal)
    assert client.post("/review/" + new + "/approve", json={}).status_code == 200


def test_cant_get_a_new_link_while_a_payment_is_in_flight(client, paypal, db_sessions):
    order = create_vault_order(client, paypal)
    mint_link(client, order)
    set_on_order(db_sessions, order, payment_claimed_at=datetime.now(timezone.utc))

    assert mint_link(client, order).status_code == 409


# ---- review links: timing ----


def saved_approval(db_sessions, order):
    with db_sessions() as session:
        return session.query(Approval).filter_by(order_id=order["id"]).one()


def test_fetching_the_page_doesnt_start_the_clock(client, paypal, db_sessions):
    # a chat app building a link preview is a GET. it shouldn't count as a human looking
    order = create_vault_order(client, paypal)
    token = mint_link(client, order).json()["review_token"]

    assert client.get("/review/" + token).status_code == 200
    assert client.get("/review/" + token).status_code == 200

    with db_sessions() as session:
        assert session.query(Approval).count() == 0
        assert session.get(Order, order["id"]).review_token_used_at is None
    assert client.get("/orders/" + str(order["id"])).json()["approval"] is None


def test_preview_then_approve_without_opened_counts_as_zero_attention(client, paypal, db_sessions):
    order = create_vault_order(client, paypal)
    token = mint_link(client, order).json()["review_token"]
    client.get("/review/" + token)
    mock_capture(paypal)

    assert client.post("/review/" + token + "/approve", json={}).status_code == 200

    saved = saved_approval(db_sessions, order)
    assert saved.decision_ms == 0
    assert saved.opened_at == saved.decided_at


def test_opened_returns_nothing_and_needs_no_key(no_key_client, client, paypal):
    order = create_vault_order(client, paypal)
    token = mint_link(client, order).json()["review_token"]

    response = no_key_client.post("/review/" + token + "/opened")

    assert response.status_code == 204
    assert response.content == b""


def test_opened_at_only_stamps_once(client, paypal, db_sessions):
    order = create_vault_order(client, paypal)
    token = mint_link(client, order).json()["review_token"]

    assert client.post("/review/" + token + "/opened").status_code == 204
    first = saved_approval(db_sessions, order)
    assert first.opened_at is not None
    # opened, not decided
    assert first.action is None
    assert first.decision_ms is None

    # move it so a second stamp couldn't land on the same value by luck
    earlier = first.opened_at - timedelta(hours=1)
    with db_sessions() as session:
        session.get(Approval, first.id).opened_at = earlier
        session.commit()

    assert client.post("/review/" + token + "/opened").status_code == 204

    # still one row, and refreshing the page can't reset the clock
    assert saved_approval(db_sessions, order).opened_at == earlier


def test_new_link_doesnt_reset_opened_at(client, paypal, db_sessions):
    order = create_vault_order(client, paypal)
    client.post("/review/" + mint_link(client, order).json()["review_token"] + "/opened")
    first = saved_approval(db_sessions, order).opened_at

    client.post("/review/" + mint_link(client, order).json()["review_token"] + "/opened")

    assert saved_approval(db_sessions, order).opened_at == first


def test_merchant_sees_an_opened_review(client, paypal):
    order = create_vault_order(client, paypal)
    client.post("/review/" + mint_link(client, order).json()["review_token"] + "/opened")

    approval = client.get("/orders/" + str(order["id"])).json()["approval"]

    assert approval["opened_at"] is not None
    assert approval["action"] is None
    assert approval["decision_ms"] is None


@pytest.mark.parametrize(("action", "mock"), [("approve", mock_capture), ("reject", mock_void)])
def test_decision_ms_is_server_computed(client, paypal, db_sessions, action, mock):
    order = create_vault_order(client, paypal)
    token = mint_link(client, order).json()["review_token"]
    client.post("/review/" + token + "/opened")

    # the human really looked for 5 seconds. the client claims it was nearly 17 minutes
    approval = saved_approval(db_sessions, order)
    opened = approval.opened_at - timedelta(seconds=5)
    with db_sessions() as session:
        session.get(Approval, approval.id).opened_at = opened
        session.commit()
    mock(paypal)

    response = client.post("/review/" + token + "/" + action, json={"decision_ms": 999999})

    assert response.status_code == 200
    saved = saved_approval(db_sessions, order)
    assert 5000 <= saved.decision_ms < 6000
    assert saved.client_decision_ms == 999999
    assert saved.opened_at == opened
    assert saved.action == ApprovalAction(action)
    # the one number is exactly the gap between the two timestamps
    assert saved.decision_ms == int((saved.decided_at - saved.opened_at).total_seconds() * 1000)


def test_client_decision_ms_is_optional(client, paypal, db_sessions):
    order = create_vault_order(client, paypal)
    token = mint_link(client, order).json()["review_token"]
    mock_capture(paypal)

    assert client.post("/review/" + token + "/approve").status_code == 200
    assert saved_approval(db_sessions, order).client_decision_ms is None


def test_tapping_without_ever_opening_counts_as_zero_attention(client, paypal, db_sessions):
    # a script going straight to approve can't get credit for time it never spent
    order = create_vault_order(client, paypal)
    token = mint_link(client, order).json()["review_token"]
    mock_capture(paypal)

    client.post("/review/" + token + "/approve", json={"decision_ms": 60000})

    saved = saved_approval(db_sessions, order)
    assert saved.decision_ms == 0
    assert saved.opened_at == saved.decided_at
    assert saved.client_decision_ms == 60000


def test_very_long_review_fits_the_column(client, paypal, db_sessions):
    # a hold lasts 29 days and an Integer tops out around 24.8. Postgres would refuse the row, after
    # PayPal had already taken the money
    order = create_vault_order(client, paypal)
    token = mint_link(client, order).json()["review_token"]
    client.post("/review/" + token + "/opened")
    approval = saved_approval(db_sessions, order)
    with db_sessions() as session:
        session.get(Approval, approval.id).opened_at = (
            approval.opened_at - timedelta(days=28)
        )
        session.commit()
    mock_capture(paypal)

    assert client.post("/review/" + token + "/approve", json={}).status_code == 200
    assert saved_approval(db_sessions, order).decision_ms == 2147483647


def test_failed_paypal_call_leaves_the_clock_running(client, paypal, db_sessions):
    order = create_vault_order(client, paypal)
    token = mint_link(client, order).json()["review_token"]
    client.post("/review/" + token + "/opened")
    opened = saved_approval(db_sessions, order).opened_at
    paypal.add(
        responses.POST,
        BASE + "/v2/payments/authorizations/0AW2184448108334S/capture",
        status=500,
        json={"name": "INTERNAL_SERVER_ERROR"},
    )

    assert client.post("/review/" + token + "/approve", json={}).status_code == 502

    saved = saved_approval(db_sessions, order)
    assert saved.action is None
    assert saved.decision_ms is None
    assert saved.opened_at == opened


# ---- network errors on create and authorize ----


NETWORK_ERRORS = [
    requests.exceptions.ConnectTimeout("too slow to connect"),
    requests.exceptions.ReadTimeout("too slow to answer"),
    requests.exceptions.ConnectionError("connection dropped"),
]


@pytest.mark.parametrize("error", NETWORK_ERRORS)
def test_network_error_on_create_is_a_clean_502(client, paypal, claude, db_sessions, caplog, error):
    paypal.add(responses.POST, BASE + "/v2/checkout/orders", body=error)
    body = dict(ORDER_BODY)
    body["vault_id"] = VAULT_ID

    with caplog.at_level(logging.ERROR):
        response = client.post("/orders", json=body)

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["message"] == "PayPal create order failed"
    assert detail["paypal_status"] is None
    assert "PayPal create order failed for order" in caplog.text
    assert claude.requests == []
    assert client.get("/orders").json()[0]["status"] == "failed"
    assert saved_decisions(db_sessions) == []


def test_network_error_on_the_vault_fallback_authorize_is_a_clean_502(client, paypal):
    paypal.add(
        responses.POST, BASE + "/v2/checkout/orders", status=201,
        json={"id": "5O190127TN364715T", "status": "APPROVED"},
    )
    paypal.add(
        responses.POST,
        BASE + "/v2/checkout/orders/5O190127TN364715T/authorize",
        body=requests.exceptions.ReadTimeout("too slow to answer"),
    )
    body = dict(ORDER_BODY)
    body["vault_id"] = VAULT_ID

    response = client.post("/orders", json=body)

    assert response.status_code == 502
    assert response.json()["detail"]["message"] == "PayPal create order failed"


@pytest.mark.parametrize("error", NETWORK_ERRORS)
def test_network_error_on_authorize_is_a_clean_502_and_can_be_retried(client, paypal, claude,
                                                                      caplog, error):
    order = create_unapproved_order(client, paypal)
    url = BASE + "/v2/checkout/orders/8XU44448NV0000000/authorize"
    paypal.add(responses.POST, url, body=error)

    with caplog.at_level(logging.ERROR):
        response = client.post("/orders/" + str(order["id"]) + "/authorize")

    assert response.status_code == 502
    assert response.json()["detail"]["message"] == "PayPal authorize failed"
    assert "PayPal authorize failed for order " + str(order["id"]) in caplog.text
    assert claude.requests == []
    assert client.get("/orders/" + str(order["id"])).json()["status"] == "created"

    paypal.replace(
        responses.POST, url, status=201,
        json=authorized_paypal_order("8XU44448NV0000000", "7PS21555RT234531W"),
    )
    retried = client.post("/orders/" + str(order["id"]) + "/authorize")

    assert retried.json()["status"] == "authorized"
    # the same id both times: if that timeout really went through, a fresh one would get a 422
    # from PayPal forever, with a hold in place that we don't know about
    calls = paypal_calls_to(paypal, "/authorize")
    assert (calls[0].request.headers["PayPal-Request-Id"]
            == calls[1].request.headers["PayPal-Request-Id"])


def test_auto_capture_is_capped_at_15_seconds(client, paypal, claude):
    claude.answer = match_answer(risk_score=5)
    mock_capture(paypal)

    assert create_vault_order(client, paypal)["review_state"] == "auto_approved"

    connect, read = paypal_calls_to(paypal, "/capture")[0].request.req_kwargs["timeout"]
    assert connect + read <= 15


def test_human_approve_gets_the_normal_paypal_timeout(client, paypal):
    # nobody's request is stacked up behind this one, so no need to cut it short
    order = create_vault_order(client, paypal)
    mock_capture(paypal)

    approve(client, order)

    assert paypal_calls_to(paypal, "/capture")[0].request.req_kwargs["timeout"] == 30


# ---- other order rules ----


def test_cant_authorize_twice(client, paypal):
    order = create_vault_order(client, paypal)

    response = client.post("/orders/" + str(order["id"]) + "/authorize")

    assert response.status_code == 409


# ---- PayPal errors and missing things ----


def test_paypal_422_debug_id_goes_to_the_log(client, paypal, caplog):
    order = create_vault_order(client, paypal)
    # Payments v2 schema example "authorizations_capture_422_expired"
    paypal.add(
        responses.POST,
        BASE + "/v2/payments/authorizations/0AW2184448108334S/capture",
        status=422,
        json={
            "name": "UNPROCCESSABLE_ENTITY",
            "message": "The requested action could not be completed, is semantically "
            "incorrect, or failed business validation.",
            "debug_id": "90bff07028f7f",
            "details": [
                {"issue": "AUTHORIZATION_EXPIRED", "description": "The authorization has expired."}
            ],
        },
    )

    with caplog.at_level(logging.ERROR):
        response = approve(client, order)

    assert response.status_code == 502
    # PayPal support asks for this, it's not something to show on a phone
    assert "90bff07028f7f" in caplog.text
    assert "AUTHORIZATION_EXPIRED" in caplog.text
    assert "debug_id" not in response.json()["detail"]
    # nothing changed on our side
    assert client.get("/orders/" + str(order["id"])).json()["status"] == "authorized"


def test_paypal_error_on_create_marks_order_failed(client, paypal):
    paypal.add(
        responses.POST,
        BASE + "/v2/checkout/orders",
        status=422,
        json={
            "name": "UNPROCESSABLE_ENTITY",
            "debug_id": "abc123",
            "details": [{"issue": "PAYEE_ACCOUNT_RESTRICTED"}],
        },
    )
    body = dict(ORDER_BODY)
    body["vault_id"] = VAULT_ID

    response = client.post("/orders", json=body)

    assert response.status_code == 502
    assert response.json()["detail"]["paypal_issue"] == "PAYEE_ACCOUNT_RESTRICTED"
    saved = client.get("/orders").json()
    assert saved[0]["status"] == "failed"


def test_missing_order_is_404(client):
    assert client.get("/orders/999").status_code == 404
    assert client.post("/orders/999/review-link").status_code == 404
    assert client.post("/orders/999/authorize").status_code == 404


def test_missing_mandate_is_404(client):
    body = dict(ORDER_BODY)
    body["mandate_id"] = 999

    assert client.post("/orders", json=body).status_code == 404


# ---- reading ----


def test_list_is_newest_first(client, paypal):
    first = create_vault_order(client, paypal, "ORDER1", "AUTH1")
    second = create_vault_order(client, paypal, "ORDER2", "AUTH2")

    listed = client.get("/orders").json()

    ids = []
    for order in listed:
        ids.append(order["id"])
    assert ids == [second["id"], first["id"]]


def test_get_order_includes_mandate_decisions_and_approvals(client, paypal, db_sessions):
    order = create_vault_order(client, paypal)
    with db_sessions() as session:
        session.add(Decision(
            order_id=order["id"],
            verdict=Verdict.mismatch,
            risk_score=70,
            reason="Socks weren't in the mandate.",
            checked_by=CheckedBy.rules,
        ))
        session.add(Approval(order_id=order["id"]))
        session.commit()

    detail = client.get("/orders/" + str(order["id"])).json()

    assert detail["mandate"]["merchant"] == "Fernwood Supply Co"
    # the first decision is the one the intent check saved when the order was authorized
    assert detail["decisions"][-1]["verdict"] == "mismatch"
    assert detail["decisions"][-1]["reason"] == "Socks weren't in the mandate."
    assert detail["approvals"][0]["action"] is None
    assert detail["items"][0]["name"] == "Trail runner"


def test_main_app_serves_order_routes():
    from backend.app.main import app

    paths = app.openapi()["paths"]
    assert "post" in paths["/orders"]
    assert "get" in paths["/orders"]
    assert "post" in paths["/orders/{order_id}/review-link"]
    assert "get" in paths["/review/{token}"]
    assert "post" in paths["/review/{token}/opened"]
    assert "post" in paths["/auth/session"]
    assert "get" in paths["/agent/orders/{order_id}"]
    assert "post" in paths["/review/{token}/approve"]
    assert "post" in paths["/review/{token}/reject"]
    assert "post" in paths["/orders/{order_id}/authorize"]
    assert "get" in paths["/orders/{order_id}"]
