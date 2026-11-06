# The rubber-stamp watch: the streak, the escalation rule, sharp reasons, and GET /stats.
# The unit tests need no database. The flow tests go through the real POST /orders and review
# links with PayPal and Claude faked, on SQLite, so nothing here reaches the network.

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from keyed_client import KeyedClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import orders, review, stats
from backend.app.db import Base, get_session
from backend.app.intent.ai_check import (
    FAILED_REASON,
    SHARP_MAX_WORDS,
    TIMED_OUT_REASON,
    limit_words,
    validate_output,
)
from backend.app.intent.check import run_intent_check
from backend.app.intent.rules import check_currency, check_expiry, check_merchant, check_price
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
    WatchMode,
)
from backend.app.paypal_client import PayPalError
from backend.app.prompts import intent as intent_prompt
from backend.app.rubber_stamp import (
    FAST_APPROVAL_MS,
    LET_THROUGH_REASON,
    LOOKBACK,
    STREAK_TO_TRIGGER,
    get_fast_streak,
    let_through_by_watch,
    should_escalate,
)

VAULT_ID = "test-vault-id"
# well in the past, so seeded decisions always sort before real taps made during a test
NOW = datetime(2026, 1, 6, 12, 0, tzinfo=timezone.utc)
SHARP_MARKER = "Sharp reason mode is on"


# ---- building blocks ----


def human_decision(action="approve", decision_ms=500):
    return Approval(action=ApprovalAction(action), decision_ms=decision_ms)


def fast():
    return human_decision("approve", 500)


def slow():
    return human_decision("approve", FAST_APPROVAL_MS)


def passed_rules():
    return [
        {"rule": "price", "passed": True, "reason": "Order is $42.00, within the $100.00 cap"},
        {"rule": "merchant", "passed": True, "reason": "Mandate store matches"},
    ]


def failed_rules():
    return [
        {"rule": "price", "passed": True, "reason": "Order is $42.00, within the $100.00 cap"},
        {"rule": "merchant", "passed": False, "reason": "Mandate says Ridgeline Outdoors"},
    ]


def check(verdict="unsure", risk_score=40, reason="Can't tell the color", rule_results=None):
    if rule_results is None:
        rule_results = passed_rules()
    return Decision(
        verdict=Verdict(verdict),
        risk_score=risk_score,
        reason=reason,
        mismatched_fields=[],
        rule_results=rule_results,
        checked_by=CheckedBy.ai,
    )


# ---- get_fast_streak ----


def test_streak_is_zero_with_no_decisions():
    assert get_fast_streak([]) == 0


def test_streak_counts_every_fast_approval():
    assert get_fast_streak([fast(), fast(), fast(), fast()]) == 4


def test_a_reject_in_the_middle_ends_the_streak():
    # newest first, so only the two in front of the reject count
    decisions = [fast(), fast(), human_decision("reject", 300), fast(), fast()]
    assert get_fast_streak(decisions) == 2


def test_a_reject_first_means_no_streak():
    assert get_fast_streak([human_decision("reject", 300), fast(), fast()]) == 0


def test_a_slow_approval_ends_the_streak():
    assert get_fast_streak([fast(), slow(), fast(), fast()]) == 1


def test_exactly_the_threshold_counts_as_slow():
    assert get_fast_streak([human_decision("approve", FAST_APPROVAL_MS - 1)]) == 1
    assert get_fast_streak([human_decision("approve", FAST_APPROVAL_MS)]) == 0


def test_missing_decision_ms_ends_the_streak():
    # no timing means we can't call it fast, so it can't feed the watch
    assert get_fast_streak([fast(), human_decision("approve", None), fast()]) == 1


def test_zero_ms_is_fast():
    # a tap on a link that was never opened is saved as 0: nobody looked at all
    assert get_fast_streak([human_decision("approve", 0)]) == 1


# ---- should_escalate ----


@pytest.mark.parametrize("watch_active", [False, True])
def test_hard_rule_failure_always_escalates(watch_active):
    # even with a score that would otherwise sail through
    failing = check(verdict="unsure", risk_score=0, rule_results=failed_rules())
    assert failing.hard_rule_failed is True
    assert should_escalate(failing, watch_active) is True


@pytest.mark.parametrize("watch_active", [False, True])
def test_mismatch_always_escalates(watch_active):
    assert should_escalate(check(verdict="mismatch", risk_score=5), watch_active) is True


@pytest.mark.parametrize("watch_active", [False, True])
@pytest.mark.parametrize("reason", [FAILED_REASON, TIMED_OUT_REASON])
def test_a_check_that_never_ran_always_escalates(watch_active, reason):
    # its 50 is a placeholder. sharp mode letting it through would capture an unchecked order
    assert should_escalate(check(verdict="unsure", risk_score=50, reason=reason), watch_active)


@pytest.mark.parametrize("watch_active", [False, True])
def test_a_missing_risk_score_escalates(watch_active):
    assert should_escalate(check(verdict="match", risk_score=None), watch_active) is True


def test_normal_mode_escalates_unsure():
    assert should_escalate(check(verdict="unsure", risk_score=10), False) is True


def test_normal_mode_keeps_the_auto_capture_limit():
    # the default AUTO_CAPTURE_MAX_RISK is 30, same rule auto_decide had before the watch
    assert should_escalate(check(verdict="match", risk_score=29), False) is False
    assert should_escalate(check(verdict="match", risk_score=30), False) is True


def test_normal_mode_follows_the_auto_capture_setting(monkeypatch):
    monkeypatch.setenv("AUTO_CAPTURE_MAX_RISK", "10")
    orders.get_settings.cache_clear()
    assert should_escalate(check(verdict="match", risk_score=15), False) is True


def test_sharp_mode_only_escalates_risky_orders():
    assert should_escalate(check(verdict="unsure", risk_score=59), True) is False
    assert should_escalate(check(verdict="unsure", risk_score=60), True) is True
    assert should_escalate(check(verdict="match", risk_score=45), True) is False


def test_sharp_vs_normal_on_the_same_order():
    middling = check(verdict="unsure", risk_score=40)
    assert should_escalate(middling, False) is True
    assert should_escalate(middling, True) is False


def test_let_through_is_only_when_the_watch_changed_the_answer():
    assert let_through_by_watch(check(verdict="unsure", risk_score=40), True) is True
    # a clean match captures either way, so the watch didn't let it through
    assert let_through_by_watch(check(verdict="match", risk_score=5), True) is False
    # escalated anyway
    assert let_through_by_watch(check(verdict="unsure", risk_score=70), True) is False
    assert let_through_by_watch(check(verdict="unsure", risk_score=40), False) is False


def test_hard_rule_failed_is_false_on_old_rows():
    assert check(rule_results=[]).hard_rule_failed is False
    old_row = check()
    old_row.rule_results = None
    assert old_row.hard_rule_failed is False


# ---- sharp reasons ----


def make_mandate(**overrides):
    values = {
        "id": 1,
        "buyer_label": "Sam",
        "raw_request": "Black trail runners, size 9, up to $100 from Fernwood Supply Co",
        "rules": {"item": "trail runners", "color": "black", "size": "9", "currency": "USD"},
        "max_price": Decimal("100.00"),
        "max_price_inclusive": True,
        "merchant": "Fernwood Supply Co",
        "expires_at": None,
    }
    values.update(overrides)
    return Mandate(**values)


def make_order(total="42.00", currency="USD"):
    return Order(
        id=1,
        items=[{"name": "Trail runner", "color": "black", "size": "9", "quantity": 1,
                "unit_price": total}],
        total=Decimal(total),
        shipping_amount=Decimal("0.00"),
        currency=currency,
    )


def word_count(text):
    return len(text.split())


def test_rule_sharp_reasons_read_mandate_said_order_has():
    price = check_price(make_mandate(), make_order(total="142.00"))
    assert price.sharp_reason == "Mandate said max $100.00, order has $142.00"

    under = check_price(make_mandate(max_price_inclusive=False), make_order(total="100.00"))
    assert under.sharp_reason == "Mandate said under $100.00, order has $100.00"

    store = check_merchant(make_mandate(merchant="Ridgeline Outdoors"))
    assert store.sharp_reason == "Mandate said Ridgeline Outdoors, order has Fernwood Supply Co"

    expired = check_expiry(make_mandate(expires_at=NOW - timedelta(days=5)), NOW)
    assert expired.sharp_reason == "Mandate said until 2026-01-01, order has 2026-01-06"

    currency = check_currency(make_mandate(rules={"currency": "EUR"}), make_order())
    assert currency.sharp_reason == "Mandate said EUR, order has USD"

    for result in [price, under, store, expired, currency]:
        assert word_count(result.sharp_reason) < 12


def test_passing_rules_have_no_sharp_reason():
    assert check_price(make_mandate(), make_order()).sharp_reason is None


def test_hard_rule_reason_is_sharp_only_in_sharp_mode():
    mandate = make_mandate(merchant="Ridgeline Outdoors")
    # price fails too, so normal mode shows its "+1 more"
    order = make_order(total="142.00")

    normal = run_intent_check(mandate, order, now=NOW)
    sharp = run_intent_check(mandate, order, now=NOW, sharp=True)

    assert normal.reason == (
        "Mandate says Ridgeline Outdoors, order is at Fernwood Supply Co (+1 more)"
    )
    # store outranks price, and the count is dropped to stay under the word limit
    assert sharp.reason == "Mandate said Ridgeline Outdoors, order has Fernwood Supply Co"
    # the record still has every failure
    assert sharp.mismatched_fields == ["price", "merchant"]


def test_a_long_store_name_still_fits_the_word_limit():
    long_name = "The Very Long Named Outdoor Gear And Trail Running Supply Emporium"
    result = run_intent_check(make_mandate(merchant=long_name), make_order(), now=NOW, sharp=True)
    assert word_count(result.reason) <= SHARP_MAX_WORDS
    assert result.reason.startswith("Mandate said The Very Long")


def test_sharp_flag_goes_in_the_prompt_after_the_order():
    mandate = make_mandate()
    order = make_order()
    normal = intent_prompt.build_user_message(mandate, order)
    sharp = intent_prompt.build_user_message(mandate, order, sharp=True)

    assert SHARP_MARKER not in normal
    assert SHARP_MARKER in sharp
    assert "Mandate said X, order has Y" in sharp
    # after the order, so text inside it can't come later and claim the last word
    assert sharp.index(SHARP_MARKER) > sharp.index("</order>")
    assert sharp.startswith(normal)


def test_sharp_mode_trims_a_long_reason_to_the_word_limit():
    answer = {
        "verdict": "mismatch",
        "risk_score": 80,
        "reason": "Mandate said black trail runners in size nine, order has olive ones in ten",
        "mismatched_fields": ["color", "size"],
    }
    sharp = validate_output(answer, sharp=True)
    normal = validate_output(answer)

    assert sharp.reason == "Mandate said black trail runners in size nine, order has olive..."
    assert word_count(sharp.reason) == SHARP_MAX_WORDS
    assert normal.reason == answer["reason"]


def test_limit_words_leaves_short_text_alone():
    assert limit_words("Mandate said black, order has olive", 11) == (
        "Mandate said black, order has olive"
    )


# ---- the order flow ----


class FakeClaude:
    # answers unsure at 40 unless a test says otherwise: escalated in normal mode, let through
    # in sharp mode, which is the difference these tests are about
    def __init__(self):
        self.answer = {
            "verdict": "unsure",
            "risk_score": 40,
            "reason": "Can't tell if it's waterproof",
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

    def last_prompt(self):
        return self.requests[-1]["messages"][0]["content"]


class FakePayPal:
    def __init__(self):
        self.capture_error = None
        self.captures = []
        self.voids = []

    def create_order(self, body, request_id=None):
        return {"id": "PP" + uuid.uuid4().hex[:12], "status": "COMPLETED", "purchase_units": [{
            "payments": {"authorizations": [{
                "id": "AUTH" + uuid.uuid4().hex[:12],
                "status": "CREATED",
                "expiration_time": "2099-11-20T01:56:55Z",
            }]}
        }]}

    def capture_authorization(self, authorization_id, request_id=None, budget_seconds=None):
        if self.capture_error:
            raise self.capture_error
        self.captures.append(authorization_id)
        return {"id": "CAP" + authorization_id, "status": "COMPLETED"}

    def void_authorization(self, authorization_id, request_id=None):
        self.voids.append(authorization_id)
        return {"id": authorization_id, "status": "VOIDED"}


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
            raw_request="Waterproof trail runners under $100 from Fernwood Supply Co",
            rules={"item": "trail runners"},
            max_price=Decimal("100.00"),
            merchant="Fernwood Supply Co",
        ))
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
def paypal():
    return FakePayPal()


@pytest.fixture
def app(db_sessions, claude, paypal):
    app = FastAPI()
    app.include_router(orders.router)
    app.include_router(review.router)
    app.include_router(stats.router)

    def test_session():
        session = db_sessions()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = test_session
    app.dependency_overrides[orders.get_paypal_client] = lambda: paypal
    app.dependency_overrides[orders.get_anthropic_client] = lambda: claude
    return app


@pytest.fixture
def client(app):
    return KeyedClient(app)


def seed_human_decisions(db_sessions, decisions):
    # decisions newest first, as (action, decision_ms). each gets its own escalated order
    with db_sessions() as session:
        for index, (action, decision_ms) in enumerate(decisions):
            decided_at = NOW - timedelta(minutes=index)
            if action == "approve":
                status, review_state = OrderStatus.captured, ReviewState.approved
            else:
                status, review_state = OrderStatus.voided, ReviewState.rejected
            order = Order(
                mandate_id=1,
                items=[{"name": "Trail runner", "quantity": 1, "unit_price": "42.00"}],
                total=Decimal("42.00"),
                currency="USD",
                status=status,
                review_state=review_state,
            )
            session.add(order)
            session.flush()
            session.add(Approval(
                order_id=order.id,
                action=ApprovalAction(action),
                opened_at=decided_at - timedelta(milliseconds=decision_ms or 0),
                decided_at=decided_at,
                decision_ms=decision_ms,
            ))
        session.commit()


def turn_watch_on(db_sessions):
    seed_human_decisions(db_sessions, [("approve", 400)] * STREAK_TO_TRIGGER)


def place_order(client, mandate_id=1, unit_price="42.00"):
    response = client.post("/orders", json={
        "mandate_id": mandate_id,
        "items": [{"name": "Trail runner", "color": "black", "size": "9", "quantity": 1,
                   "unit_price": unit_price}],
        "vault_id": VAULT_ID,
    })
    assert response.status_code == 201, response.text
    return client.get("/orders/" + str(response.json()["id"])).json()


def test_normal_mode_is_unchanged(client, claude):
    order = place_order(client)

    assert order["watch_mode"] == "normal"
    assert order["status"] == "authorized"
    assert order["review_state"] == "needs_review"
    assert order["intent_check"]["reason"] == "Can't tell if it's waterproof"
    assert SHARP_MARKER not in claude.last_prompt()


def test_a_clean_match_captures_in_normal_mode_with_its_own_reason(client, claude):
    claude.answer = {"verdict": "match", "risk_score": 5, "reason": "Matches the mandate.",
                     "mismatched_fields": []}
    order = place_order(client)

    assert order["status"] == "captured"
    assert order["review_state"] == "auto_approved"
    assert order["intent_check"]["reason"] == "Matches the mandate."


def test_two_fast_approvals_arent_enough(client, db_sessions, claude):
    seed_human_decisions(db_sessions, [("approve", 400)] * (STREAK_TO_TRIGGER - 1))
    order = place_order(client)

    assert order["watch_mode"] == "normal"
    assert order["review_state"] == "needs_review"


def test_a_recent_reject_keeps_the_watch_off(client, db_sessions):
    seed_human_decisions(db_sessions, [("reject", 400)] + [("approve", 400)] * 5)
    assert place_order(client)["watch_mode"] == "normal"


def test_sharp_mode_lets_a_low_risk_unsure_order_through(client, db_sessions, claude, paypal):
    turn_watch_on(db_sessions)
    order = place_order(client)

    assert order["watch_mode"] == "sharp"
    assert order["status"] == "captured"
    assert order["review_state"] == "auto_approved"
    # auditable: the dashboard says why nobody was asked
    assert order["intent_check"]["reason"] == LET_THROUGH_REASON
    # what Claude actually judged is still on record
    assert order["intent_check"]["verdict"] == "unsure"
    assert order["intent_check"]["risk_score"] == 40
    assert len(paypal.captures) == 1
    # one call, with the flag in it
    assert len(claude.requests) == 1
    assert SHARP_MARKER in claude.last_prompt()


def test_sharp_mode_still_escalates_risky_orders_with_a_sharp_reason(client, db_sessions, claude):
    turn_watch_on(db_sessions)
    claude.answer = {"verdict": "unsure", "risk_score": 65,
                     "reason": "Mandate said waterproof, order has no mention",
                     "mismatched_fields": []}
    order = place_order(client)

    assert order["watch_mode"] == "sharp"
    assert order["review_state"] == "needs_review"
    assert order["intent_check"]["reason"] == "Mandate said waterproof, order has no mention"


def test_sharp_mode_escalates_every_mismatch(client, db_sessions, claude, paypal):
    turn_watch_on(db_sessions)
    claude.answer = {"verdict": "mismatch", "risk_score": 20,
                     "reason": "Mandate said black, order has olive",
                     "mismatched_fields": ["color"]}
    order = place_order(client)

    assert order["review_state"] == "needs_review"
    assert order["intent_check"]["reason"] == "Mandate said black, order has olive"
    assert paypal.captures == []


def test_sharp_mode_escalates_a_hard_rule_failure_with_a_sharp_reason(client, db_sessions, claude):
    turn_watch_on(db_sessions)
    order = place_order(client, mandate_id=2)

    assert order["watch_mode"] == "sharp"
    assert order["review_state"] == "needs_review"
    assert order["intent_check"]["reason"] == (
        "Mandate said Ridgeline Outdoors, order has Fernwood Supply Co"
    )
    # the rules caught it, Claude never ran
    assert claude.requests == []


def test_sharp_mode_never_captures_when_claude_fails(client, db_sessions, claude, paypal):
    turn_watch_on(db_sessions)
    claude.error = RuntimeError("Claude is down")
    order = place_order(client)

    assert order["review_state"] == "needs_review"
    assert order["intent_check"]["reason"] == FAILED_REASON
    assert paypal.captures == []


def test_a_failed_capture_keeps_claudes_reason(client, db_sessions, paypal):
    # the let-through reason only lands with the money. this one goes to a human, who needs
    # Claude's reason, not a note saying it was let through
    turn_watch_on(db_sessions)
    paypal.capture_error = PayPalError(500, message="Internal error")
    order = place_order(client)

    assert order["status"] == "authorized"
    assert order["review_state"] == "needs_review"
    assert order["intent_check"]["reason"] == "Can't tell if it's waterproof"


def test_a_clean_match_in_sharp_mode_keeps_its_own_reason(client, db_sessions, claude):
    turn_watch_on(db_sessions)
    claude.answer = {"verdict": "match", "risk_score": 5, "reason": "Matches the mandate.",
                     "mismatched_fields": []}
    order = place_order(client)

    assert order["status"] == "captured"
    assert order["intent_check"]["reason"] == "Matches the mandate."


def test_real_fast_taps_turn_the_watch_on(client):
    # end to end through review links: tapping without opening the page saves 0 ms
    for _ in range(STREAK_TO_TRIGGER):
        order = place_order(client)
        token = client.post("/orders/" + str(order["id"]) + "/review-link").json()["review_token"]
        assert client.post("/review/" + token + "/approve").status_code == 200

    assert place_order(client)["watch_mode"] == "sharp"


def test_a_real_reject_turns_it_back_off(client, db_sessions):
    turn_watch_on(db_sessions)
    risky = place_order(client, mandate_id=2)
    token = client.post("/orders/" + str(risky["id"]) + "/review-link").json()["review_token"]
    assert client.post("/review/" + token + "/reject").status_code == 200

    assert place_order(client)["watch_mode"] == "normal"


def test_agents_never_see_the_watch_mode(client, db_sessions):
    turn_watch_on(db_sessions)
    response = client.post("/orders", json={
        "mandate_id": 1,
        "items": [{"name": "Trail runner", "quantity": 1, "unit_price": "42.00"}],
        "vault_id": VAULT_ID,
    })
    assert set(response.json()) == {"id", "status", "review_state", "total"}


# ---- GET /stats ----


STATS_FIELDS = {
    "total_orders", "auto_captured", "needs_review", "approved", "voided",
    "median_decision_ms", "fast_streak", "rubber_stamp_active", "fast_threshold_ms",
    "recent_decision_ms",
}


def test_stats_shape_on_an_empty_database(client):
    response = client.get("/stats")
    assert response.status_code == 200
    body = response.json()

    assert set(body) == STATS_FIELDS
    assert body == {
        "total_orders": 0,
        "auto_captured": 0,
        "needs_review": 0,
        "approved": 0,
        "voided": 0,
        "median_decision_ms": None,
        "fast_streak": 0,
        "rubber_stamp_active": False,
        "fast_threshold_ms": FAST_APPROVAL_MS,
        "recent_decision_ms": [],
    }


def test_stats_counts_and_timings(client, db_sessions, claude):
    # newest first: three fast approvals, then a slow one and a reject
    seed_human_decisions(db_sessions, [
        ("approve", 300), ("approve", 900), ("approve", 1500), ("approve", 8000), ("reject", 4000),
    ])
    # one auto-capture and one waiting
    claude.answer = {"verdict": "match", "risk_score": 5, "reason": "Matches.",
                     "mismatched_fields": []}
    place_order(client)
    place_order(client, mandate_id=2)

    body = client.get("/stats").json()
    assert set(body) == STATS_FIELDS
    assert body["total_orders"] == 7
    assert body["auto_captured"] == 1
    assert body["needs_review"] == 1
    assert body["approved"] == 4
    assert body["voided"] == 1
    assert body["median_decision_ms"] == 1500
    assert body["fast_streak"] == 3
    assert body["rubber_stamp_active"] is True
    assert body["recent_decision_ms"] == [300, 900, 1500, 8000, 4000]


def test_stats_median_of_an_even_count_is_whole_ms(client, db_sessions):
    seed_human_decisions(db_sessions, [("approve", 1000), ("approve", 2001)])
    assert client.get("/stats").json()["median_decision_ms"] == 1500


def test_stats_only_shows_the_last_ten(client, db_sessions):
    timings = []
    for index in range(LOOKBACK + 3):
        timings.append(("approve", 3000 + index))
    seed_human_decisions(db_sessions, timings)

    recent = client.get("/stats").json()["recent_decision_ms"]
    assert len(recent) == LOOKBACK
    assert recent[0] == 3000
    assert recent[-1] == 3000 + LOOKBACK - 1


def test_stats_needs_the_merchant(app):
    no_key = TestClient(app)
    assert no_key.get("/stats").status_code == 401
    assert no_key.get("/stats", headers={"X-API-Key": "test-agent-key"}).status_code == 401


def test_stats_and_the_order_flow_agree(client, db_sessions):
    turn_watch_on(db_sessions)
    assert client.get("/stats").json()["rubber_stamp_active"] is True
    assert place_order(client)["watch_mode"] == WatchMode.sharp.value
