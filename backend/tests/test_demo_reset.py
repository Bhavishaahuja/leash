# POST /demo/reset: void the seeded holds at PayPal, then delete every seeded row. The dashboard
# calls it with a session, seed_demo.py --reset with the merchant key. PayPal is faked here.

from datetime import datetime, timedelta, timezone

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient
from keyed_client import AGENT_HEADERS, MERCHANT_HEADERS
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import demo, orders
from backend.app.auth import issue_session_token
from backend.app.db import Base, get_session
from backend.app.demo import RESET_MAX_TRIES, SEED_LABEL_PREFIX, reset_limiter
from backend.app.models import (
    Approval,
    Decision,
    Dispute,
    Mandate,
    Order,
    OrderStatus,
    ReviewState,
    Verdict,
)


class FakePayPal:
    def __init__(self):
        self.voids = []
        # authorization ids whose void fails, like PayPal being down for just that call
        self.failing = set()

    def void_authorization(self, authorization_id, request_id=None):
        if authorization_id in self.failing:
            raise requests.ConnectionError("PayPal is down")
        self.voids.append((authorization_id, request_id))
        return {"id": authorization_id, "status": "VOIDED"}


@pytest.fixture
def sessions():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    # Postgres refuses to delete an order a dispute still points at. SQLite only does if asked
    @event.listens_for(engine, "connect")
    def enforce_foreign_keys(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def paypal():
    return FakePayPal()


@pytest.fixture
def client(sessions, paypal):
    app = FastAPI()
    app.include_router(demo.router)
    app.include_router(orders.router)

    def test_session():
        session = sessions()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = test_session
    app.dependency_overrides[orders.get_paypal_client] = lambda: paypal
    return TestClient(app)


@pytest.fixture
def dashboard_headers():
    token, _ = issue_session_token()
    return {"Authorization": "Bearer " + token}


def later(days=1):
    return datetime.now(timezone.utc) + timedelta(days=days)


def add_mandate(session, label):
    mandate = Mandate(buyer_label=label, raw_request="x", rules={})
    session.add(mandate)
    session.flush()
    return mandate.id


def add_order(session, mandate_id, status, review_state, expires_at=None, claimed_at=None):
    order = Order(
        mandate_id=mandate_id, items=[], total=10, currency="USD", status=status,
        review_state=review_state, authorization_id="AUTH-" + str(session.query(Order).count()),
        authorization_expires_at=expires_at, payment_claimed_at=claimed_at,
    )
    session.add(order)
    session.flush()
    return order.id


@pytest.fixture
def seeded(sessions):
    # what a seed run leaves behind, plus one real order that the reset must not touch
    ids = {}
    with sessions() as session:
        seed = add_mandate(session, SEED_LABEL_PREFIX + "clean: camping mug")
        sneaky = add_mandate(session, SEED_LABEL_PREFIX + "sneaky: wrong color")
        real = add_mandate(session, "Sam")

        ids["captured"] = add_order(session, seed, OrderStatus.captured, ReviewState.auto_approved)
        ids["open_1"] = add_order(
            session, sneaky, OrderStatus.authorized, ReviewState.needs_review, later()
        )
        ids["open_2"] = add_order(
            session, sneaky, OrderStatus.authorized, ReviewState.needs_review, later()
        )
        # expired at PayPal already, nothing left to void
        ids["expired"] = add_order(
            session, sneaky, OrderStatus.authorized, ReviewState.needs_review, later(-1)
        )
        ids["real_open"] = add_order(
            session, real, OrderStatus.authorized, ReviewState.needs_review, later()
        )

        session.add(Decision(order_id=ids["open_1"], verdict=Verdict.mismatch, reason="Wrong",
                             checked_by="ai"))
        session.add(Approval(order_id=ids["captured"]))
        session.add(Dispute(paypal_dispute_id="PP-R-TEST-1", order_id=ids["captured"]))
        ids["real_mandate"] = real
        session.commit()
    return ids


def reset(client, headers, address="10.0.0.1"):
    headers = dict(headers)
    headers["X-Forwarded-For"] = address
    return client.post("/demo/reset", headers=headers)


# ---- what it does ----


def test_reset_voids_the_open_holds_then_deletes_everything_seeded(
    client, sessions, paypal, seeded, dashboard_headers
):
    response = reset(client, dashboard_headers)

    assert response.status_code == 200
    assert response.json() == {
        "voided_holds": 2,
        "voided_order_ids": [seeded["open_1"], seeded["open_2"]],
        "deleted_orders": 4,
        "deleted_mandates": 2,
        "deleted_disputes": 1,
    }
    with sessions() as session:
        assert session.query(Order).count() == 1
        assert session.query(Mandate).count() == 1
        assert session.query(Decision).count() == 0
        assert session.query(Approval).count() == 0
        assert session.query(Dispute).count() == 0


def test_only_live_seeded_holds_are_voided(client, paypal, seeded, dashboard_headers):
    reset(client, dashboard_headers)

    voided = []
    for authorization_id, _ in paypal.voids:
        voided.append(authorization_id)
    # not the captured one, not the expired one, and never the real shopper's
    assert len(voided) == 2
    assert "AUTH-0" not in voided


def test_a_real_order_is_left_exactly_as_it_was(client, sessions, seeded, dashboard_headers):
    reset(client, dashboard_headers)

    with sessions() as session:
        order = session.get(Order, seeded["real_open"])
        assert order.status == OrderStatus.authorized
        assert order.review_state == ReviewState.needs_review
        assert session.get(Mandate, seeded["real_mandate"]) is not None


def test_voids_use_the_derived_request_id_so_a_retry_cant_double_up(
    client, sessions, paypal, seeded, dashboard_headers
):
    with sessions() as session:
        expected = []
        for key in ["open_1", "open_2"]:
            order = session.get(Order, seeded[key])
            expected.append(orders.payment_request_id(order, orders.VOID))

    reset(client, dashboard_headers)

    sent = []
    for _, request_id in paypal.voids:
        sent.append(request_id)
    assert sent == expected


def test_resetting_with_nothing_seeded_is_fine(client, paypal, dashboard_headers):
    response = reset(client, dashboard_headers)

    assert response.status_code == 200
    assert response.json() == {"voided_holds": 0, "voided_order_ids": [], "deleted_orders": 0,
                               "deleted_mandates": 0, "deleted_disputes": 0}
    assert paypal.voids == []


def test_a_second_reset_has_nothing_left_to_do(client, paypal, seeded, dashboard_headers):
    reset(client, dashboard_headers)

    second = reset(client, dashboard_headers).json()

    assert second["voided_holds"] == 0
    assert second["deleted_orders"] == 0
    assert len(paypal.voids) == 2


# ---- when it has to stop ----


def test_a_failed_void_stops_before_deleting_anything(
    client, sessions, paypal, seeded, dashboard_headers
):
    paypal.failing.add("AUTH-2")

    response = reset(client, dashboard_headers)

    assert response.status_code == 502
    assert response.json()["detail"]["order_id"] == seeded["open_2"]
    with sessions() as session:
        # every row still there, so the hold PayPal still has isn't forgotten
        assert session.query(Order).count() == 5
        failed = session.get(Order, seeded["open_2"])
        assert failed.status == OrderStatus.authorized
        # claim let go, so the next reset can try it again
        assert failed.payment_claimed_at is None
        # the one before it really was voided, and stays that way
        assert session.get(Order, seeded["open_1"]).status == OrderStatus.voided


def test_running_it_again_after_paypal_recovers_finishes_the_job(
    client, sessions, paypal, seeded, dashboard_headers
):
    paypal.failing.add("AUTH-2")
    reset(client, dashboard_headers)
    paypal.failing.clear()

    response = reset(client, dashboard_headers)

    assert response.status_code == 200
    assert response.json()["voided_order_ids"] == [seeded["open_2"]]
    with sessions() as session:
        assert session.query(Order).count() == 1


def test_an_order_a_human_is_deciding_right_now_stops_the_reset(
    client, sessions, paypal, seeded, dashboard_headers
):
    # a human just tapped approve and the capture is on its way to PayPal. voiding now could race
    # it, so the reset backs off
    with sessions() as session:
        order = session.get(Order, seeded["open_1"])
        order.payment_claimed_at = datetime.now(timezone.utc)
        session.commit()

    response = reset(client, dashboard_headers)

    assert response.status_code == 409
    assert response.json()["detail"]["order_id"] == seeded["open_1"]
    assert paypal.voids == []
    with sessions() as session:
        assert session.query(Order).count() == 5


# ---- who can call it ----


def test_the_merchant_key_works_too(client, seeded):
    # what seed_demo.py --reset sends
    assert reset(client, MERCHANT_HEADERS).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [{}, AGENT_HEADERS, {"X-API-Key": "wrong"}, {"Authorization": "Bearer 123.not-signed"}],
)
def test_anyone_else_gets_a_401_and_nothing_happens(client, sessions, paypal, seeded, headers):
    assert reset(client, headers).status_code == 401
    assert paypal.voids == []
    with sessions() as session:
        assert session.query(Order).count() == 5


# ---- the rate limit ----


def test_a_few_resets_a_minute_then_a_429(client, dashboard_headers):
    for _ in range(RESET_MAX_TRIES):
        assert reset(client, dashboard_headers).status_code == 200

    response = reset(client, dashboard_headers)

    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


def test_the_limit_is_per_address(client, dashboard_headers):
    for _ in range(RESET_MAX_TRIES):
        reset(client, dashboard_headers, address="10.0.0.1")

    assert reset(client, dashboard_headers, address="10.0.0.1").status_code == 429
    assert reset(client, dashboard_headers, address="10.0.0.2").status_code == 200


def test_a_limited_reset_touches_nothing(client, sessions, paypal, seeded, dashboard_headers):
    for _ in range(RESET_MAX_TRIES):
        reset_limiter.seconds_until_allowed("10.0.0.1")

    assert reset(client, dashboard_headers).status_code == 429
    assert paypal.voids == []
    with sessions() as session:
        assert session.query(Order).count() == 5


def test_calls_without_a_login_dont_use_up_the_allowance(client, dashboard_headers):
    # otherwise anyone could lock the judges out by hammering it with no session
    for _ in range(10):
        assert reset(client, {}).status_code == 401

    assert reset(client, dashboard_headers).status_code == 200


def test_the_window_lets_go_after_a_minute():
    for _ in range(RESET_MAX_TRIES):
        reset_limiter.seconds_until_allowed("10.0.0.9", now=1000.0)

    assert reset_limiter.seconds_until_allowed("10.0.0.9", now=1030.0) > 0
    assert reset_limiter.seconds_until_allowed("10.0.0.9", now=1061.0) == 0


# ---- DELETE /demo/seeded still works on its own ----


def test_delete_seeded_still_refuses_while_holds_are_open(client, seeded):
    response = client.delete("/demo/seeded", headers=MERCHANT_HEADERS)

    assert response.status_code == 409
    assert sorted(response.json()["detail"]["open_order_ids"]) == [
        seeded["open_1"], seeded["open_2"],
    ]
