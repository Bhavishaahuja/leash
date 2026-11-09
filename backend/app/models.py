# the tables Leash needs. field choices follow spikes/README.md (disputes: spike 3)

import enum
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.app.db import Base


class OrderStatus(str, enum.Enum):
    created = "created"
    authorized = "authorized"
    captured = "captured"
    voided = "voided"
    failed = "failed"


class Verdict(str, enum.Enum):
    match = "match"
    mismatch = "mismatch"
    unsure = "unsure"


class CheckedBy(str, enum.Enum):
    rules = "rules"
    ai = "ai"


class ApprovalAction(str, enum.Enum):
    approve = "approve"
    reject = "reject"


class ReviewState(str, enum.Enum):
    # who decided what happens to the money. Order.status is where the money actually is
    auto_approved = "auto_approved"
    needs_review = "needs_review"
    approved = "approved"
    rejected = "rejected"


class WatchMode(str, enum.Enum):
    # which mode the rubber-stamp watch was in when this order got checked, see rubber_stamp.py
    normal = "normal"
    # the human had been approving too fast: fewer escalations, sharper reasons
    sharp = "sharp"


def string_enum(enum_class, name):
    # store enums as plain strings with a CHECK constraint instead of a Postgres enum
    # type, so adding a value later is a simple migration
    return Enum(
        enum_class,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=20,
        values_callable=lambda members: [member.value for member in members],
    )


def created_at_column():
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Mandate(Base):
    # what the human told their agent it's allowed to buy
    __tablename__ = "mandates"

    id: Mapped[int] = mapped_column(primary_key=True)
    buyer_label: Mapped[str] = mapped_column(String(200))
    # the human's own words, kept verbatim for dispute evidence
    raw_request: Mapped[str] = mapped_column(Text)
    # structured version Claude/the rules check against (categories, limits, etc)
    rules: Mapped[dict] = mapped_column(JSON)
    max_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    # True: total may equal max_price ("up to $120"). False: must be below it ("under $120")
    max_price_inclusive: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default=true()
    )
    merchant: Mapped[str | None] = mapped_column(String(200))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()

    orders: Mapped[list["Order"]] = relationship(back_populates="mandate")


class Order(Base):
    # one agent order and where its PayPal money is at
    __tablename__ = "orders"
    # named, so the migration can drop it again on any database
    __table_args__ = (UniqueConstraint("review_token_hash", name="uq_orders_review_token_hash"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    mandate_id: Mapped[int] = mapped_column(ForeignKey("mandates.id"), index=True)
    paypal_order_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    authorization_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # disputes point at this (disputed_transactions[].seller_transaction_id), see spike 3
    capture_id: Mapped[str | None] = mapped_column(String(64), index=True)
    vault_id: Mapped[str | None] = mapped_column(String(64))
    # the store the order is at. null means ours, which is every real agent order. only
    # POST /demo/orders sets it, so the merchant rule has a wrong store to catch
    merchant: Mapped[str | None] = mapped_column(String(200))
    items: Mapped[list] = mapped_column(JSON)
    total: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    # the part of total that's shipping. null on orders saved before this column existed,
    # we don't know their split and 0 would be a made-up number
    shipping_amount: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    status: Mapped[OrderStatus] = mapped_column(
        string_enum(OrderStatus, "order_status"), default=OrderStatus.created
    )
    # null until there's a hold to decide about, needs_review from the moment there is one
    review_state: Mapped[ReviewState | None] = mapped_column(
        string_enum(ReviewState, "review_state")
    )
    # PayPal's status from voiding the hold, like VOIDED. null if it was never voided
    void_result: Mapped[str | None] = mapped_column(String(32))
    # set while a capture or void is on its way to PayPal, so a second tap can't send another one
    # without holding a row lock for that whole call. see claim_order in orders.py
    payment_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # SHA-256 of the current review link's token. the token itself is never stored, so a leaked
    # database can't approve anything. the link dies with authorization_expires_at
    review_token_hash: Mapped[str | None] = mapped_column(String(64))
    # when the link was used. set in the same commit as the payment claim
    review_token_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # PayPal authorizations last 29 days (spike 2), the human has to decide before this
    authorization_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # set when the order gets checked. orders that never got that far (no hold yet, or PayPal
    # failed) were never judged in sharp mode, so normal is true for them too
    watch_mode: Mapped[WatchMode] = mapped_column(
        string_enum(WatchMode, "watch_mode"),
        default=WatchMode.normal,
        server_default=WatchMode.normal.value,
        nullable=False,
    )
    created_at: Mapped[datetime] = created_at_column()

    mandate: Mapped[Mandate] = relationship(back_populates="orders")
    # oldest first. the database doesn't promise any order unless it's asked for one
    decisions: Mapped[list["Decision"]] = relationship(
        back_populates="order", order_by="Decision.id"
    )
    approvals: Mapped[list["Approval"]] = relationship(
        back_populates="order", order_by="Approval.id"
    )

    @property
    def intent_check(self):
        # the newest check result, or None if the order hasn't been checked. reads the saved row
        # so API responses always show what's actually in the database
        if not self.decisions:
            return None
        return self.decisions[-1]

    @property
    def approval(self):
        # the human's review, or None if the confirm page hasn't rendered. action stays empty on
        # a review that's been opened but not decided yet
        if not self.approvals:
            return None
        return self.approvals[-1]


class Decision(Base):
    # the mandate check result for an order
    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    verdict: Mapped[Verdict] = mapped_column(string_enum(Verdict, "verdict"))
    # 0 to 100, higher means more likely the human didn't want this
    risk_score: Mapped[int | None] = mapped_column(Integer)
    # the one-line reason the human sees
    reason: Mapped[str] = mapped_column(Text)
    # what differs: rule names when the rules caught it, otherwise what Claude listed.
    # these three are null on rows saved before the check was wired in
    mismatched_fields: Mapped[list | None] = mapped_column(JSON)
    # every hard rule's result as {rule, passed, reason}, including the ones that passed
    rule_results: Mapped[list | None] = mapped_column(JSON)
    # how long the whole check took
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    checked_by: Mapped[CheckedBy] = mapped_column(string_enum(CheckedBy, "checked_by"))
    created_at: Mapped[datetime] = created_at_column()

    order: Mapped[Order] = relationship(back_populates="decisions")

    @property
    def hard_rule_failed(self):
        # any of the deterministic rules said no. null on rows saved before rule_results existed
        if not self.rule_results:
            return False
        for result in self.rule_results:
            if not result.get("passed"):
                return True
        return False


class Approval(Base):
    # the human looking at a flagged order. timing feeds the rubber-stamp watch
    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    # empty until the human actually taps approve or reject
    action: Mapped[ApprovalAction | None] = mapped_column(
        string_enum(ApprovalAction, "approval_action")
    )
    decided_by: Mapped[str | None] = mapped_column(String(200))
    # stamped by the server the first time the confirm page says it rendered, never moved after
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # when the tap reached the server
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # decided_at minus opened_at. the rubber-stamp watch reads this one, because both ends are
    # the server's own clock and a lying client can't fake attention
    decision_ms: Mapped[int | None] = mapped_column(Integer)
    # what the client said it measured, kept only to compare against. never trusted
    client_decision_ms: Mapped[int | None] = mapped_column(Integer)

    order: Mapped[Order] = relationship(back_populates="approvals")


class Dispute(Base):
    # a PayPal dispute on one of our captured orders, and the evidence Leash filed for it.
    # reason and status are PayPal's own strings (UNAUTHORISED, WAITING_FOR_SELLER_RESPONSE...),
    # kept as plain text since PayPal can add values without telling us
    __tablename__ = "disputes"

    id: Mapped[int] = mapped_column(primary_key=True)
    # like PP-R-ABC-10001234. unique, so a second sync updates the row instead of copying it
    paypal_dispute_id: Mapped[str] = mapped_column(String(64), unique=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    reason: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str | None] = mapped_column(String(64))
    # the summary Claude drafted for the PayPal reviewer. the merchant reads it on GET .../evidence
    # and that exact text is what submit sends as the notes
    evidence_summary: Mapped[str | None] = mapped_column(Text)
    # what we filed as, e.g. OTHER. null until submitted
    evidence_type: Mapped[str | None] = mapped_column(String(64))
    # set while a submit is on its way to PayPal, so a second one can't go too. same idea as
    # Order.payment_claimed_at
    evidence_claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # PayPal took the evidence. once set, submit refuses
    evidence_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # PayPal's seller_response_due_date: miss it and the dispute closes in the buyer's favor.
    # null when PayPal isn't waiting on us (e.g. while it's under review)
    seller_response_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = created_at_column()

    order: Mapped[Order] = relationship()


class CatalogItem(Base):
    # one sellable variant at the demo store: a size and color of a sneaker, one model of earbuds,
    # one date of an event. the demo scenarios pick a sibling variant from here to get wrong
    __tablename__ = "catalog_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    # one of the fixed names in catalog.py (CATEGORIES), the parser is told to use the same ones
    category: Mapped[str] = mapped_column(String(50), index=True)
    # the variant fields. empty when the product doesn't come in variants of that kind
    size: Mapped[str | None] = mapped_column(String(50))
    color: Mapped[str | None] = mapped_column(String(50))
    model: Mapped[str | None] = mapped_column(String(100))
    event_date: Mapped[date | None] = mapped_column(Date)
    # what one unit of quantity means: "pair", "carton", "ticket", "month"
    quantity_unit: Mapped[str] = mapped_column(String(20))
    # the list price for one unit
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    in_stock: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    merchant: Mapped[str] = mapped_column(String(200))
