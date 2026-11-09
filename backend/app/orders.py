# Agent orders: create (PayPal intent AUTHORIZE), authorize, list, read. The human's side of it,
# review links plus approve and reject, is in review.py.
# Response shapes are documented for the frontend in docs/api.md, keep the two in step.
#
# Money flow is what the spikes proved in sandbox:
#   spike 1: create -> buyer approves -> authorize -> capture or void
#   spike 2: create with payment_source.paypal.vault_id -> comes back already authorized,
#            no buyer action. falls back to /authorize if it ever doesn't
#
# status is where the money is, and moves one way only:
#   created -> authorized -> captured
#                         -> voided
#   any PayPal failure while creating/authorizing, or a declined capture -> failed
#
# review_state is who decided. It starts at needs_review the moment there's a hold:
#   needs_review -> auto_approved    clean match below the risk limit, captured right away
#                -> approved         a human tapped approve, captured
#                -> rejected         a human tapped reject, voided
#
# Starting at needs_review means anything going wrong (a crash, a failed save, a PayPal error)
# leaves the order waiting for a human with no money moved. Capture only ever happens by
# something actively deciding to. There's deliberately no plain capture or void endpoint, it
# would let an agent settle its own flagged order.

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from typing import Annotated

import requests
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, selectinload

from backend.app import mandate_parser, rubber_stamp
from backend.app.auth import require_agent, require_merchant
from backend.app.config import STORE_NAME, get_settings
from backend.app.db import get_session
from backend.app.intent.check import build_decision, run_intent_check
from backend.app.intent.rules import as_utc
from backend.app.models import (
    Approval,
    ApprovalAction,
    CheckedBy,
    Mandate,
    Order,
    OrderStatus,
    ReviewState,
    Verdict,
    WatchMode,
)
from backend.app.paypal_client import PayPalError, client_from_settings, first_authorization

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orders", tags=["orders"])
agent_router = APIRouter(prefix="/agent", tags=["agent"])

CENT = Decimal("0.01")
CURRENCY = "USD"

CAPTURE = "capture"
VOID = "void"

# timeouts and dropped connections come through as requests errors, not PayPalError
PAYPAL_FAILURES = (PayPalError, requests.RequestException)

# long enough to outlast a slow PayPal call (30s for the token plus 30s for the call). after this
# a claim counts as abandoned by a crashed request, and retrying is safe because the same
# PayPal-Request-Id gets the original result back
CLAIM_EXPIRES_AFTER = timedelta(seconds=120)

# the agent is waiting on POST /orders for this one, so it gets less time than a human's tap. if
# it runs out, the order goes to review, and approving there can't charge twice: same request id
AUTO_CAPTURE_BUDGET_SECONDS = 15

# the most an Integer column holds, about 24.8 days. a hold lasts 29, so a real review can pass it
MAX_DECISION_MS = 2147483647


# ---- request / response shapes ----


class OrderItemIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    color: str | None = Field(default=None, max_length=100)
    size: str | None = Field(default=None, max_length=100)
    # which model, like "FP-200". its own field so the check can name it as what differs
    model: str | None = Field(default=None, max_length=100)
    # the day of the event, for tickets and bookings. "2026-12-05"
    event_date: date | None = None
    quantity: int = Field(ge=1, le=1000)
    unit_price: Decimal = Field(gt=0, max_digits=10, decimal_places=2)


class OrderCreate(BaseModel):
    mandate_id: int
    items: list[OrderItemIn] = Field(min_length=1, max_length=50)
    shipping: Decimal = Field(default=Decimal("0"), ge=0, max_digits=10, decimal_places=2)
    # saved PayPal wallet from spike 2's flow. with it, no buyer popup
    vault_id: str | None = Field(default=None, max_length=64)


class RuleResultOut(BaseModel):
    rule: str
    passed: bool
    reason: str


class DecisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    verdict: Verdict
    risk_score: int | None
    reason: str
    # these three are null on decisions saved before the check was wired in
    mismatched_fields: list[str] | None
    rule_results: list[RuleResultOut] | None
    latency_ms: int | None
    checked_by: CheckedBy
    created_at: datetime


class MandateSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    buyer_label: str
    raw_request: str
    rules: dict
    max_price: Decimal | None
    max_price_inclusive: bool
    merchant: str | None
    expires_at: datetime | None
    created_at: datetime


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    action: ApprovalAction | None
    decided_by: str | None
    opened_at: datetime | None
    decided_at: datetime | None
    # server-measured, decided_at minus opened_at
    decision_ms: int | None
    # what the client claimed. only here to compare against
    client_decision_ms: int | None


class OrderOut(BaseModel):
    # the merchant's view of an order, see docs/api.md
    model_config = ConfigDict(from_attributes=True)

    id: int
    mandate_id: int
    paypal_order_id: str | None
    authorization_id: str | None
    capture_id: str | None
    void_result: str | None
    vault_id: str | None
    # null means our own store. only a demo order names another one
    merchant: str | None
    items: list
    total: Decimal
    shipping_amount: Decimal | None
    currency: str
    status: OrderStatus
    # null until there's a hold to decide about
    review_state: ReviewState | None
    authorization_expires_at: datetime | None
    # whether the rubber-stamp watch was on when this was checked. merchant only: an agent that
    # could see sharp mode would know when doubtful orders stop being escalated
    watch_mode: WatchMode
    created_at: datetime
    mandate: MandateSummary
    # the newest check result. null until the order is checked, or if saving the result failed
    intent_check: DecisionOut | None
    # the human's review. null until the link is opened, action is null until they decide
    approval: ApprovalOut | None


class AgentOrderStatus(BaseModel):
    # all the agent ever gets to see of an order: where it stands. these four fields are the whole
    # shape, so a token or a reason can't come back here even by accident
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: OrderStatus
    review_state: ReviewState | None
    total: Decimal


class OrderPlacedNeedsBuyer(AgentOrderStatus):
    # no vault id, so the buyer has to open this and approve at PayPal before there's a hold. the
    # one thing on top of the four fields, since the order can't go anywhere without it
    payer_action_url: str


class OrderDetail(OrderOut):
    # the full history, for GET /orders/{id}
    decisions: list[DecisionOut]
    approvals: list[ApprovalOut]


# ---- dependencies ----


@lru_cache
def get_paypal_client():
    # one client for the whole app so the OAuth token cache is shared between requests
    return client_from_settings(get_settings())


@lru_cache
def get_anthropic_client():
    # shared for the same reason, and so tests can swap in a fake. with no API key this still
    # builds fine, the failure comes at call time and the check fails closed
    return mandate_parser.make_client(get_settings().anthropic_api_key)


DbSession = Annotated[Session, Depends(get_session)]
PayPal = Annotated[object, Depends(get_paypal_client)]
Claude = Annotated[object, Depends(get_anthropic_client)]


# ---- helpers ----


def money(value):
    return str(value.quantize(CENT, rounding=ROUND_HALF_UP))


def item_display_name(item):
    # "Trail runner, black, size 9". PayPal item names are free text
    parts = [item.name]
    if item.color:
        parts.append(item.color)
    if item.size:
        # the agent may already have written "size women's 9", don't make it "size size ..."
        size = item.size.strip()
        if size.lower().startswith("size"):
            parts.append(size)
        else:
            parts.append("size " + size)
    if item.model:
        parts.append("model " + item.model.strip())
    if item.event_date:
        parts.append(format_event_date(item.event_date))
    return ", ".join(parts)


def format_event_date(event_date):
    # "Dec 5, 2026". built by hand because %-d doesn't exist on Windows
    return event_date.strftime("%b") + " " + str(event_date.day) + ", " + str(event_date.year)


def build_paypal_order(order_id, items, shipping, vault_id, settings):
    # Orders v2 create body. With items, amount.breakdown.item_total has to equal the sum of
    # unit_amount * quantity, and amount.value has to equal item_total + shipping
    paypal_items = []
    item_total = Decimal("0")
    for item in items:
        paypal_items.append({
            "name": item_display_name(item),
            "quantity": str(item.quantity),
            "unit_amount": {"currency_code": CURRENCY, "value": money(item.unit_price)},
            "category": "PHYSICAL_GOODS",
        })
        item_total += item.unit_price * item.quantity

    total = item_total + shipping

    purchase_unit = {
        "reference_id": "leash-order-" + str(order_id),
        # our id rides along so PayPal records (and disputes) point back at this row
        "custom_id": str(order_id),
        "description": STORE_NAME + " agent order",
        "amount": {
            "currency_code": CURRENCY,
            "value": money(total),
            "breakdown": {
                "item_total": {"currency_code": CURRENCY, "value": money(item_total)},
                "shipping": {"currency_code": CURRENCY, "value": money(shipping)},
            },
        },
        "items": paypal_items,
    }

    if vault_id:
        # same as spike 2's order 2: merchant starts a later payment on a saved wallet
        payment_source = {
            "paypal": {
                "vault_id": vault_id,
                "stored_credential": {
                    "payment_initiator": "MERCHANT",
                    "usage": "SUBSEQUENT",
                    "usage_pattern": "IMMEDIATE",
                },
            }
        }
    else:
        # same as spike 1: the buyer approves in the browser first
        payment_source = {
            "paypal": {
                "experience_context": {
                    "brand_name": STORE_NAME,
                    "user_action": "PAY_NOW",
                    "shipping_preference": "GET_FROM_FILE",
                    "return_url": settings.paypal_return_url,
                    "cancel_url": settings.paypal_cancel_url,
                }
            }
        }

    body = {
        "intent": "AUTHORIZE",
        "purchase_units": [purchase_unit],
        "payment_source": payment_source,
    }
    return body, total.quantize(CENT)


def payer_action_link(paypal_order):
    # with payment_source set PayPal says "payer-action", older flows say "approve"
    for link in paypal_order.get("links", []):
        if link.get("rel") in ("payer-action", "approve"):
            return link.get("href")
    return None


def parse_paypal_time(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def paypal_error_response(action, error):
    # PayPal said no, or we never heard back. Pass on what it said (debug_id is what PayPal support
    # asks for), as a 502 since the failure is upstream of us. a timeout has none of those details
    # and they stay null, the message is the part to show
    detail = {
        "message": "PayPal " + action + " failed",
        "paypal_status": None,
        "paypal_name": None,
        "paypal_issue": None,
        "paypal_message": None,
        "debug_id": None,
    }
    if isinstance(error, PayPalError):
        detail["paypal_status"] = error.status_code
        detail["paypal_name"] = error.name
        detail["paypal_issue"] = error.issue
        detail["paypal_message"] = error.message
        detail["debug_id"] = error.debug_id
    return HTTPException(status_code=502, detail=detail)


def apply_authorization(order, authorization):
    # CREATED means the hold is in place. anything else (DENIED, PENDING...) we can't capture
    order.authorization_id = authorization.get("id")
    order.authorization_expires_at = parse_paypal_time(authorization.get("expiration_time"))
    if authorization.get("status") == "CREATED":
        order.status = OrderStatus.authorized
        # in the same commit as the hold, so there's never a hold nobody is responsible for
        order.review_state = ReviewState.needs_review
    else:
        order.status = OrderStatus.failed


def load_order(session, order_id, lock=False):
    query = select(Order).where(Order.id == order_id)
    if lock:
        # row lock on Postgres so two requests can't both pass the state checks. the lock alone
        # would still hand back the old values of an order this session already loaded, and
        # those old values are what would get checked, so ask for fresh ones
        query = query.with_for_update().execution_options(populate_existing=True)
    order = session.scalars(query).first()
    if order is None:
        raise HTTPException(status_code=404, detail="Order " + str(order_id) + " not found")
    return order


def require_status(order, allowed, action):
    if order.status in allowed:
        return
    reasons = {
        OrderStatus.created: "isn't authorized yet",
        OrderStatus.authorized: "is already authorized",
        OrderStatus.captured: "is already captured",
        OrderStatus.voided: "was voided",
        OrderStatus.failed: "failed at PayPal",
    }
    raise HTTPException(
        status_code=409,
        detail="Can't " + action + " order " + str(order.id) + ": it "
        + reasons[order.status] + " (status " + order.status.value + ")",
    )


def utc_now():
    return datetime.now(timezone.utc)


def payment_request_id(order, action):
    # the same order and action always gets the same PayPal-Request-Id, so a retry gets PayPal's
    # original result and can't capture twice. the authorization id is in there because order ids
    # restart in every database, and local and Render share one sandbox account: on its own,
    # order 1 here could get back the capture of a different order 1 over there
    seed = "leash:" + str(order.id) + ":" + str(order.authorization_id) + ":" + action
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def authorize_request_id(paypal_order_id):
    # same idea. if an authorize times out but really went through, retrying with a fresh id gets
    # a 422 from PayPal forever, and the order sits at created with a hold nobody knows about
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "leash:" + paypal_order_id + ":authorize"))


def review_blocker(order):
    # why nobody can settle this order right now, or None if they can
    if order.status != OrderStatus.authorized or order.review_state != ReviewState.needs_review:
        return "Order " + str(order.id) + " isn't waiting for review"

    if order.payment_claimed_at is not None:
        if utc_now() - as_utc(order.payment_claimed_at) < CLAIM_EXPIRES_AFTER:
            return "Order " + str(order.id) + " already has a payment in progress"
    return None


def claim_order(session, order_id):
    # lock, check, mark it taken, commit. the commit lets go of the lock before PayPal gets
    # called, so no connection waits on PayPal, and the claim is what stops a second request:
    # it now sees the claim and gets a 409 straight away, instead of queueing on the lock.
    # returns (order, reason it can't be claimed). the lock is still held if there is a reason
    order = load_order(session, order_id, lock=True)
    blocker = review_blocker(order)
    if blocker is None:
        order.payment_claimed_at = utc_now()
        session.commit()
    return order, blocker


def release_claim(session, order_id):
    # PayPal failed, so let someone try again. that goes for the review link too: it was used up
    # with the claim, and a human stuck on a 502 shouldn't need a new link to retry. if this fails
    # as well, the claim just expires and the merchant can create a new link
    try:
        order = load_order(session, order_id, lock=True)
        order.payment_claimed_at = None
        order.review_token_used_at = None
        session.commit()
    except SQLAlchemyError:
        session.rollback()
        logger.exception("Couldn't release the payment claim on order %s", order_id)


def capture_payment(paypal, order, budget_seconds=None):
    capture = paypal.capture_authorization(
        order.authorization_id,
        request_id=payment_request_id(order, CAPTURE),
        budget_seconds=budget_seconds,
    )
    # it's the id that disputes need later, so an answer without one isn't a usable capture
    if not capture or not capture.get("id"):
        raise PayPalError(200, message="Capture response had no id")
    return capture


@dataclass(frozen=True)
class ReviewTap:
    action: ApprovalAction
    # when the tap reached the server
    tapped_at: datetime
    # what the client says it measured, can be None
    client_decision_ms: int | None


def record_tap(session, order_id, tap):
    # fills in the row opening the link created. the only time decision_ms is ever set
    query = (
        select(Approval)
        .where(Approval.order_id == order_id, Approval.action.is_(None))
        .order_by(Approval.id.desc())
    )
    approval = session.scalars(query).first()
    if approval is None:
        # never opened, so no time spent looking: opened_at is the tap itself and decision_ms is 0
        approval = Approval(order_id=order_id, opened_at=tap.tapped_at)
        session.add(approval)

    elapsed = tap.tapped_at - as_utc(approval.opened_at)
    decision_ms = int(elapsed.total_seconds() * 1000)

    approval.action = tap.action
    approval.decided_at = tap.tapped_at
    approval.decision_ms = max(0, min(decision_ms, MAX_DECISION_MS))
    approval.client_decision_ms = tap.client_decision_ms


def finish_capture(session, order_id, capture, review_state, tap=None, check_reason=None):
    # returns True if the money was taken. a decline leaves review_state as it was.
    # check_reason replaces the intent check's reason, in the same commit as the capture, so the
    # record can't say one thing while the money says another
    order = load_order(session, order_id, lock=True)
    order.capture_id = capture.get("id")
    order.payment_claimed_at = None

    captured = capture.get("status") != "DECLINED"
    if captured:
        # COMPLETED, or PENDING while PayPal finishes. either way the money is taken
        order.status = OrderStatus.captured
        order.review_state = review_state
        if tap is not None:
            record_tap(session, order_id, tap)
        if check_reason is not None and order.intent_check is not None:
            order.intent_check.reason = check_reason
    else:
        order.status = OrderStatus.failed
    session.commit()
    return captured


def auto_decide(session, order_id, paypal, watch_active=False):
    # the order is already needs_review, so this only ever has to do something to move money.
    # returning early is the safe outcome for every odd case, including a null intent_check
    order = load_order(session, order_id)
    check = order.intent_check
    if check is None:
        return
    if rubber_stamp.should_escalate(check, watch_active):
        return

    # normal mode would have sent this one to a human. the dashboard has to say why it didn't,
    # but only once the money is actually taken, a failed capture still goes to review
    check_reason = None
    if rubber_stamp.let_through_by_watch(check, watch_active):
        check_reason = rubber_stamp.LET_THROUGH_REASON

    order, blocker = claim_order(session, order_id)
    if blocker is not None:
        # a human got here first, in the seconds the check was running
        session.rollback()
        logger.info("Skipping auto-capture: %s", blocker)
        return

    try:
        capture = capture_payment(paypal, order, budget_seconds=AUTO_CAPTURE_BUDGET_SECONDS)
    except PAYPAL_FAILURES:
        # no retry in here: the agent is waiting, and a human can approve it now
        logger.exception("Auto-capture failed for order %s, sending to review", order_id)
        release_claim(session, order_id)
        return

    try:
        captured = finish_capture(
            session, order_id, capture, ReviewState.auto_approved, check_reason=check_reason
        )
    except SQLAlchemyError:
        # stays needs_review with the claim on it. once that expires, approving sends the same
        # PayPal-Request-Id, gets this same capture back, and saves it
        session.rollback()
        logger.exception(
            "Order %s was captured at PayPal as %s but saving that failed",
            order_id, capture.get("id"),
        )
        return

    if not captured:
        logger.error("PayPal declined the auto-capture of order %s", order_id)


def check_and_decide(session, order, paypal, claude):
    order_id = order.id

    # before the check, not just before auto-decide: the check needs to know too, it's what
    # writes the sharp reason. same answer for both, so the two can't disagree
    watch_active = watch_for(session, order_id)
    if watch_active:
        order.watch_mode = WatchMode.sharp
    else:
        order.watch_mode = WatchMode.normal

    # check_and_save's first commit saves watch_mode along with everything else
    check_and_save(session, order, claude, sharp=watch_active)
    # the hold is committed and the agent is about to get an answer. failing here would look like
    # the purchase failed and could make it buy again, and the order is safely in needs_review
    try:
        auto_decide(session, order_id, paypal, watch_active)
    except SQLAlchemyError:
        session.rollback()
        logger.exception("Auto-decide failed for order %s, sending to review", order_id)


def watch_for(session, order_id):
    # if we can't read the history, normal mode is the safe guess: it escalates more, not less
    try:
        return rubber_stamp.watch_is_on(session)
    except SQLAlchemyError:
        session.rollback()
        logger.exception("Couldn't read the rubber-stamp watch for order %s", order_id)
        return False


def check_and_save(session, order, claude, sharp=False):
    # the order was committed as authorized before this runs, so whatever happens in here the
    # hold is on record and it's waiting for review
    mandate = order.mandate

    # Claude can take up to 20 seconds. ending the transaction hands the connection back to the
    # pool instead of sitting on one that long. objects stay usable, expire_on_commit is off
    session.commit()

    result = run_intent_check(mandate, order, client=claude, sharp=sharp)

    # a failed save can't fail the request: the agent would see an error and may buy again,
    # placing a second hold. the order just comes back with intent_check null
    try:
        session.add(build_decision(order, result))
        session.commit()
    except SQLAlchemyError:
        session.rollback()
        logger.exception("Couldn't save the intent check for order %s", order.id)


def item_to_json(item):
    # how an OrderItemIn is saved on Order.items, and so what the intent check reads.
    # backend/evals/run_evals.py builds its orders with this too, so they look like real ones
    return {
        "name": item.name,
        "color": item.color,
        "size": item.size,
        "model": item.model,
        "event_date": item.event_date.isoformat() if item.event_date else None,
        "quantity": item.quantity,
        "unit_price": money(item.unit_price),
    }


def place_order(session, mandate, items, shipping, vault_id, paypal, claude, merchant=None):
    # the whole path an order takes: save, hold at PayPal, check, auto-decide. POST /orders and
    # POST /demo/orders both land here, so the demo can't drift from what an agent's order does.
    # merchant stays None for a real agent order, those are always at our one store.
    # returns (order, payer_action_url), and the url is None unless a buyer has to approve first
    items_json = []
    for item in items:
        items_json.append(item_to_json(item))

    # save first so the PayPal order can carry our id
    order = Order(
        mandate_id=mandate.id,
        items=items_json,
        total=Decimal("0"),
        # quantized so a defaulted "0" still reads back as 0.00
        shipping_amount=shipping.quantize(CENT),
        currency=CURRENCY,
        vault_id=vault_id,
        merchant=merchant,
        status=OrderStatus.created,
    )
    session.add(order)
    session.flush()

    paypal_body, total = build_paypal_order(
        order.id, items, shipping, vault_id, get_settings()
    )
    order.total = total

    try:
        paypal_order = paypal.create_order(paypal_body)
        order.paypal_order_id = paypal_order.get("id")

        payer_action_url = None
        if vault_id:
            # spike 2: a vault_id order comes back COMPLETED with the authorization inside.
            # if this sandbox ever returns it un-authorized, authorize it ourselves
            authorization = first_authorization(paypal_order)
            if authorization is None:
                paypal_order = paypal.authorize_order(
                    order.paypal_order_id,
                    request_id=authorize_request_id(order.paypal_order_id),
                )
                authorization = first_authorization(paypal_order)
            if authorization is None:
                order.status = OrderStatus.failed
            else:
                apply_authorization(order, authorization)
        else:
            payer_action_url = payer_action_link(paypal_order)
    except PAYPAL_FAILURES as error:
        logger.exception("PayPal create order failed for order %s", order.id)
        order.status = OrderStatus.failed
        session.commit()
        raise paypal_error_response("create order", error) from error

    session.commit()

    if order.status == OrderStatus.authorized:
        check_and_decide(session, order, paypal, claude)

    return order, payer_action_url


# ---- endpoints ----


@router.post(
    "",
    response_model=OrderPlacedNeedsBuyer | AgentOrderStatus,
    status_code=201,
    dependencies=[Depends(require_agent)],
)
def create_order(body: OrderCreate, session: DbSession, paypal: PayPal, claude: Claude):
    mandate = session.get(Mandate, body.mandate_id)
    if mandate is None:
        raise HTTPException(
            status_code=404, detail="Mandate " + str(body.mandate_id) + " not found"
        )

    order, payer_action_url = place_order(
        session, mandate, body.items, body.shipping, body.vault_id, paypal, claude
    )

    # the agent doesn't get the reason or anything else from the check, or it could read why it was
    # flagged, adjust, and try again
    placed = AgentOrderStatus.model_validate(order)
    if payer_action_url:
        return OrderPlacedNeedsBuyer(**placed.model_dump(), payer_action_url=payer_action_url)
    return placed


@router.post(
    "/{order_id}/authorize",
    response_model=AgentOrderStatus,
    dependencies=[Depends(require_agent)],
)
def authorize_order(order_id: int, session: DbSession, paypal: PayPal, claude: Claude):
    # the no-vault path: buyer approved at the payer-action link, now place the hold
    order = load_order(session, order_id, lock=True)
    require_status(order, [OrderStatus.created], "authorize")

    try:
        paypal_order = paypal.authorize_order(
            order.paypal_order_id, request_id=authorize_request_id(order.paypal_order_id)
        )
    except PAYPAL_FAILURES as error:
        # e.g. 422 ORDER_NOT_APPROVED when the buyer hasn't clicked through yet, or a timeout.
        # leave the order as created so it can be retried
        logger.exception("PayPal authorize failed for order %s", order_id)
        session.rollback()
        raise paypal_error_response("authorize", error) from error

    authorization = first_authorization(paypal_order)
    if authorization is None:
        order.status = OrderStatus.failed
    else:
        apply_authorization(order, authorization)
    session.commit()

    if order.status == OrderStatus.authorized:
        check_and_decide(session, order, paypal, claude)
    return order


@router.get("", response_model=list[OrderOut], dependencies=[Depends(require_merchant)])
def list_orders(session: DbSession, limit: int = 50):
    limit = max(1, min(limit, 200))
    # each order's response includes all of these, so load them in one go, not one per row
    query = (
        select(Order)
        .options(
            selectinload(Order.mandate),
            selectinload(Order.decisions),
            selectinload(Order.approvals),
        )
        .order_by(Order.created_at.desc(), Order.id.desc())
        .limit(limit)
    )
    return session.scalars(query).all()


@router.get("/{order_id}", response_model=OrderDetail, dependencies=[Depends(require_merchant)])
def get_order(order_id: int, session: DbSession):
    return load_order(session, order_id)


@agent_router.get(
    "/orders/{order_id}", response_model=AgentOrderStatus, dependencies=[Depends(require_agent)]
)
def get_agent_order_status(order_id: int, session: DbSession):
    return load_order(session, order_id)
