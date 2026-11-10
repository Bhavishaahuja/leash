# The human's side of an order: review links, and approving or rejecting through one.
#
# The link is what stands in for a login. Whoever holds it can decide that one order and nothing
# else, and the agent never gets to see one:
#   - POST /orders/{id}/review-link needs the merchant key, and it's the only place the raw token
#     ever appears. only its SHA-256 is stored, so it can't be read back later. calling it again
#     makes a new link and kills the old one, which is how a lost link gets recovered
#   - the link dies with the PayPal authorization, since there's nothing left to decide after that
#   - approving or rejecting uses it up, in the same commit as the payment claim
#
# Timing is measured here too. POST .../opened stamps opened_at once, and decision_ms is the
# server's own clock from there to the tap. It's a separate call and not part of GET because chat
# apps fetch links to build previews, and that would start the clock before anyone had looked. The
# client's own number gets saved next to it but nothing trusts it.

import hashlib
import logging
import secrets
from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from backend.app import orders
from backend.app.auth import require_merchant
from backend.app.intent.rules import as_utc
from backend.app.models import (
    Approval,
    ApprovalAction,
    CatalogItem,
    Order,
    OrderStatus,
    ReviewState,
    Verdict,
)
from backend.app.orders import DbSession, PayPal, utc_now

logger = logging.getLogger(__name__)

router = APIRouter(tags=["review"])

# 32 random bytes, 43 characters in the URL
TOKEN_BYTES = 32


# ---- request / response shapes ----


class ReviewLink(BaseModel):
    # only ever appears in this one response. it can't be fetched again
    review_token: str
    # when the link stops working, the same moment the PayPal hold runs out
    expires_at: datetime


class ReviewMandate(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    buyer_label: str
    raw_request: str


class ReviewCheck(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    verdict: Verdict
    risk_score: int | None
    reason: str
    mismatched_fields: list[str] | None
    # what the agent should have bought, so the human has something better than "no" to go on
    suggested_fix: orders.SuggestedFixOut | None = None


class ReviewView(BaseModel):
    # what the confirm page needs and nothing else. whoever holds a link doesn't need PayPal ids,
    # the vault id, or how long reviews usually take
    model_config = ConfigDict(from_attributes=True)

    id: int
    items: list
    total: Decimal
    shipping_amount: Decimal | None
    currency: str
    status: OrderStatus
    review_state: ReviewState | None
    authorization_expires_at: datetime | None
    mandate: ReviewMandate
    intent_check: ReviewCheck | None
    # set once the human accepted the suggested fix: this order was voided and replaced by that one
    replaced_by_order_id: int | None = None


class ReplacementOrder(BaseModel):
    # the order placed for the accepted fix. what the human needs to see happened to it, nothing
    # more: it went through the same check and auto-decide as any order
    model_config = ConfigDict(from_attributes=True)

    id: int
    items: list
    total: Decimal
    currency: str
    status: OrderStatus
    review_state: ReviewState | None


class AcceptFixResult(BaseModel):
    # the original, now voided, and what replaced it
    order: ReviewView
    replacement: ReplacementOrder


class ReviewTapBody(BaseModel):
    # what the client measured, page open to tap. optional and never trusted, see the top of file
    decision_ms: int | None = Field(default=None, ge=0, le=orders.MAX_DECISION_MS)


OptionalTap = ReviewTapBody | None


# ---- helpers ----


def hash_token(token):
    # plain SHA-256 is fine here: the token is 32 random bytes, so there's nothing to brute force
    return hashlib.sha256(token.encode()).hexdigest()


def load_order_by_token(session, token, lock=True):
    # locked unless it's only being read, because opening and using a link both change the row
    query = select(Order).where(Order.review_token_hash == hash_token(token))
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return session.scalars(query).first()


def hold_has_expired(order):
    # no expiry on record means we can't say the hold is still good, so that counts as expired
    if order.authorization_expires_at is None:
        return True
    return utc_now() >= as_utc(order.authorization_expires_at)


def link_error(session, status_code, message):
    # lets go of the lock too. the message goes straight on the phone
    session.rollback()
    return HTTPException(status_code=status_code, detail={"message": message})


def review_conflict(session, order, message):
    # build it before the rollback, which throws away the values it reads
    conflict = HTTPException(
        status_code=409,
        detail={
            "message": message,
            "status": order.status.value,
            "review_state": order.review_state.value if order.review_state else None,
        },
    )
    session.rollback()
    return conflict


def load_live_order(session, token, lock=True):
    order = load_order_by_token(session, token, lock=lock)
    if order is None:
        raise link_error(session, 404, "This review link isn't valid")
    if hold_has_expired(order):
        raise link_error(session, 410, "This review link has expired")
    return order


def load_open_link_order(session, token, lock=True):
    # for the page and the opened stamp: there's nothing to show once the link is done with
    order = load_live_order(session, token, lock=lock)
    if order.review_token_used_at is not None:
        raise link_error(session, 410, "This review link has already been used")
    if order.review_state != ReviewState.needs_review:
        raise link_error(session, 410, "This order isn't waiting for review anymore")
    return order


def claim_by_token(session, token):
    # the lock is what makes this safe: a second tap waits here, then sees what this one wrote.
    # using up the link and claiming the payment land in one commit, there's no moment with one
    # and not the other
    order = load_live_order(session, token)

    # the state message comes first because it tells a double tap what actually happened
    blocker = orders.review_blocker(order)
    if blocker is not None:
        raise review_conflict(session, order, blocker)
    if order.review_token_used_at is not None:
        raise review_conflict(session, order, "This review link has already been used")

    now = utc_now()
    order.review_token_used_at = now
    order.payment_claimed_at = now
    session.commit()
    return order


def payment_failed(action):
    # nothing changed: if we got this far the order was authorized and waiting for review. these
    # are known values, not read from the order, because the database may be what just failed.
    # the debug_id is in the log, the phone only needs to know to try again
    return HTTPException(
        status_code=502,
        detail={
            "message": "PayPal " + action + " failed, the order is still waiting for review",
            "status": OrderStatus.authorized.value,
            "review_state": ReviewState.needs_review.value,
        },
    )


def save_failed(action, order_id):
    # PayPal did it but we couldn't record it. a new link gets the same PayPal-Request-Id, so
    # going again returns the same result and saves it
    return HTTPException(
        status_code=502,
        detail={"message": "PayPal " + action + " went through but order " + str(order_id)
                + " couldn't be updated. Ask for a new link in a couple of minutes"},
    )


def client_ms(body):
    if body is None:
        return None
    return body.decision_ms


# ---- endpoints ----


@router.post(
    "/orders/{order_id}/review-link",
    response_model=ReviewLink,
    status_code=201,
    dependencies=[Depends(require_merchant)],
)
def create_review_link(order_id: int, session: DbSession):
    order = orders.load_order(session, order_id, lock=True)

    blocker = orders.review_blocker(order)
    if blocker is not None:
        raise review_conflict(session, order, blocker)
    if hold_has_expired(order):
        raise review_conflict(session, order, "The PayPal authorization has expired")

    token = secrets.token_urlsafe(TOKEN_BYTES)
    order.review_token_hash = hash_token(token)
    order.review_token_used_at = None
    expires_at = order.authorization_expires_at
    session.commit()
    return ReviewLink(review_token=token, expires_at=expires_at)


@router.get("/review/{token}", response_model=ReviewView)
def open_review(token: str, session: DbSession):
    # read only, so a link preview or a refresh changes nothing
    return load_open_link_order(session, token, lock=False)


@router.post("/review/{token}/opened", status_code=204)
def mark_opened(token: str, session: DbSession):
    # first call only. the row lock means two at once can't both get through, and calling it again
    # can't reset the clock and make a quick tap look considered
    order = load_open_link_order(session, token)

    query = select(Approval).where(Approval.order_id == order.id).order_by(Approval.id.desc())
    if session.scalars(query).first() is None:
        session.add(Approval(order_id=order.id, opened_at=utc_now()))
    session.commit()


@router.post("/review/{token}/approve", response_model=ReviewView)
def approve_order(token: str, session: DbSession, paypal: PayPal, body: OptionalTap = None):
    tapped_at = utc_now()
    order = claim_by_token(session, token)
    order_id = order.id

    try:
        capture = orders.capture_payment(paypal, order)
    except orders.PAYPAL_FAILURES as error:
        # e.g. 422 AUTHORIZATION_EXPIRED. PayPal's record wins, nothing changes on our side
        logger.exception("PayPal capture failed for order %s", order_id)
        orders.release_claim(session, order_id)
        raise payment_failed(orders.CAPTURE) from error

    tap = orders.ReviewTap(ApprovalAction.approve, tapped_at, client_ms(body))
    try:
        captured = orders.finish_capture(
            session, order_id, capture, ReviewState.approved, tap
        )
    except SQLAlchemyError as error:
        session.rollback()
        logger.exception(
            "Order %s was captured at PayPal as %s but saving that failed",
            order_id, capture.get("id"),
        )
        raise save_failed(orders.CAPTURE, order_id) from error

    if not captured:
        logger.error("PayPal declined the capture of order %s", order_id)
        raise HTTPException(
            status_code=502,
            detail={
                "message": "PayPal declined the capture",
                "status": OrderStatus.failed.value,
                "review_state": ReviewState.needs_review.value,
            },
        )
    return order


@router.post("/review/{token}/reject", response_model=ReviewView)
def reject_order(token: str, session: DbSession, paypal: PayPal, body: OptionalTap = None):
    tapped_at = utc_now()
    order = claim_by_token(session, token)
    order_id = order.id

    try:
        voided = paypal.void_authorization(
            order.authorization_id, request_id=orders.payment_request_id(order, orders.VOID)
        )
    except orders.PAYPAL_FAILURES as error:
        logger.exception("PayPal void failed for order %s", order_id)
        orders.release_claim(session, order_id)
        raise payment_failed(orders.VOID) from error

    tap = orders.ReviewTap(ApprovalAction.reject, tapped_at, client_ms(body))
    try:
        order = orders.load_order(session, order_id, lock=True)
        order.status = OrderStatus.voided
        order.review_state = ReviewState.rejected
        # the client asks for the full body so this is normally PayPal's own status, but a 204
        # with no body is also a documented success
        order.void_result = voided.get("status") if voided else "VOIDED"
        order.payment_claimed_at = None
        orders.record_tap(session, order_id, tap)
        session.commit()
    except SQLAlchemyError as error:
        session.rollback()
        logger.exception("Order %s was voided at PayPal but saving that failed", order_id)
        raise save_failed(orders.VOID, order_id) from error
    return order


# ---- accepting the suggested fix ----


class FixUnavailable(Exception):
    # the suggested fix can't be placed. found before any money moves, so nothing has changed
    pass


def replacement_items(session, order):
    # the one item the replacement order is for, checked against the catalog as it is right now.
    # raises FixUnavailable for anything that would stop the swap, before the void
    check = order.intent_check
    fix = check.suggested_fix if check is not None else None
    if not fix:
        raise FixUnavailable("There's no suggested fix for this order")
    # no buyer can approve a new payment from here, so the replacement has to go on the same saved
    # wallet. an order paid through the PayPal popup doesn't have one
    if not order.vault_id:
        raise FixUnavailable("This order wasn't paid with a saved wallet, so it can't be replaced")

    row = session.scalars(select(CatalogItem).where(CatalogItem.sku == fix["sku"])).first()
    if row is None or not row.in_stock:
        raise FixUnavailable("The suggested item isn't in stock any more")
    # the human agreed to the price on the screen. if the catalog changed since, they haven't
    if row.price != Decimal(str(fix["price"])):
        raise FixUnavailable("The suggested item's price changed since this page loaded")

    try:
        item = orders.OrderItemIn(
            name=row.name, color=row.color, size=row.size, model=row.model,
            event_date=row.event_date, quantity=fix["quantity"], unit_price=row.price,
        )
    except ValidationError as error:
        raise FixUnavailable("The suggested fix isn't a valid order") from error
    return [item]


def replacement_failed(voided_order_id, replacement_order_id, paypal_detail=None):
    # the void went through and stays: nobody is charged for the original. what failed is the new
    # order, so the human hears exactly that, and that nothing was charged for it either
    detail = {
        "message": "The original hold was voided, but the replacement order failed. Nothing "
        "was charged. The shopper's agent will need to order again.",
        "voided_order_id": voided_order_id,
        "status": OrderStatus.voided.value,
        "replacement_order_id": replacement_order_id,
    }
    if paypal_detail is not None:
        detail["paypal"] = paypal_detail
    return HTTPException(status_code=502, detail=detail)


def replacement_order_id_for(session, voided_order_id):
    # the replacement row is saved before PayPal is called, so even a failed one can be found
    query = select(Order.id).where(Order.replaces_order_id == voided_order_id)
    return session.scalars(query).first()


@router.post("/review/{token}/accept-fix", response_model=AcceptFixResult)
def accept_fix(
    token: str, session: DbSession, paypal: PayPal, claude: orders.Claude,
    body: OptionalTap = None,
):
    # void this order and place the suggested fix instead, on the same saved wallet and for the
    # same agent. same rules as approve and reject: the link works once, the claim stops a double
    # tap, and decision_ms comes from the server's clock
    tapped_at = utc_now()
    order = claim_by_token(session, token)
    order_id = order.id

    # everything that could stop the swap, before any money moves. if one of these says no, the
    # link and the hold are both left as they were
    try:
        items = replacement_items(session, order)
    except FixUnavailable as error:
        orders.release_claim(session, order_id)
        raise HTTPException(status_code=409, detail={"message": str(error)}) from error

    try:
        voided = paypal.void_authorization(
            order.authorization_id, request_id=orders.payment_request_id(order, orders.VOID)
        )
    except orders.PAYPAL_FAILURES as error:
        logger.exception("PayPal void failed for order %s while accepting its fix", order_id)
        orders.release_claim(session, order_id)
        raise payment_failed(orders.VOID) from error

    # the void and the human's tap land in one commit, before the new order is tried. from here
    # the original hold is gone, whatever happens to the replacement
    tap = orders.ReviewTap(ApprovalAction.accept_fix, tapped_at, client_ms(body))
    try:
        order = orders.load_order(session, order_id, lock=True)
        order.status = OrderStatus.voided
        order.review_state = ReviewState.rejected
        order.void_result = voided.get("status") if voided else "VOIDED"
        order.payment_claimed_at = None
        orders.record_tap(session, order_id, tap)
        session.commit()
    except SQLAlchemyError as error:
        session.rollback()
        logger.exception("Order %s was voided at PayPal but saving that failed", order_id)
        raise save_failed(orders.VOID, order_id) from error

    # the same path as any agent order: hold on the saved wallet, the intent check, auto-decide.
    # a fix that turns out not to match after all goes to a human like any other order
    try:
        replacement, _ = orders.place_order(
            session, order.mandate, items, order.shipping_amount or Decimal("0"), order.vault_id,
            paypal, claude, merchant=order.merchant, agent_name=order.agent_name,
            replaces_order_id=order_id,
        )
    except HTTPException as error:
        # PayPal refused the new order. place_order saved it as failed before raising
        logger.error("Replacement for voided order %s failed at PayPal", order_id)
        session.rollback()
        new_id = replacement_order_id_for(session, order_id)
        link_replacement(session, order_id, new_id)
        raise replacement_failed(order_id, new_id, error.detail) from error
    except Exception as error:
        logger.exception("Replacement for voided order %s failed", order_id)
        session.rollback()
        new_id = replacement_order_id_for(session, order_id)
        link_replacement(session, order_id, new_id)
        raise replacement_failed(order_id, new_id) from error

    link_replacement(session, order_id, replacement.id)

    # PayPal took the order but never authorized it: no hold, so nothing was checked or charged
    if replacement.status == OrderStatus.failed:
        raise replacement_failed(order_id, replacement.id)

    voided_order = orders.load_order(session, order_id)
    return AcceptFixResult(
        order=ReviewView.model_validate(voided_order),
        replacement=ReplacementOrder.model_validate(replacement),
    )


def link_replacement(session, voided_order_id, replacement_order_id):
    # the new order already points back. this is the forward link, and losing it isn't worth
    # failing a request whose money has already moved
    if replacement_order_id is None:
        return
    try:
        voided_order = orders.load_order(session, voided_order_id, lock=True)
        voided_order.replaced_by_order_id = replacement_order_id
        session.commit()
    except SQLAlchemyError:
        session.rollback()
        logger.exception(
            "Couldn't link order %s to its replacement %s", voided_order_id, replacement_order_id
        )
