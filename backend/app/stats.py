# GET /stats: the dashboard's numbers, including where the rubber-stamp watch stands.
# Merchant only. The agent never learns whether the watch is on, or it could time its orders.

import statistics

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select

from backend.app import rubber_stamp
from backend.app.auth import require_merchant
from backend.app.models import Approval, Order, OrderStatus, ReviewState
from backend.app.orders import DbSession

router = APIRouter(tags=["stats"])


class StatsOut(BaseModel):
    total_orders: int
    # captured by Leash itself, no human involved
    auto_captured: int
    # holds waiting on a human right now
    needs_review: int
    # a human tapped approve
    approved: int
    # where the money is, so this counts every void
    voided: int
    # over every human decision with a server timing. null until there is one
    median_decision_ms: int | None
    fast_streak: int
    rubber_stamp_active: bool
    fast_threshold_ms: int
    # the last LOOKBACK human decisions, newest first. null where an old row has no timing
    recent_decision_ms: list[int | None]


def count_orders(session, *conditions):
    query = select(func.count()).select_from(Order)
    for condition in conditions:
        query = query.where(condition)
    return session.scalar(query)


def median_decision_ms(session):
    query = select(Approval.decision_ms).where(
        Approval.action.is_not(None), Approval.decision_ms.is_not(None)
    )
    timings = list(session.scalars(query).all())
    if not timings:
        return None
    # an even count gives the halfway point, round it back to whole milliseconds
    return round(statistics.median(timings))


@router.get("/stats", response_model=StatsOut, dependencies=[Depends(require_merchant)])
def get_stats(session: DbSession):
    # the same list the order flow uses, so the dashboard and the next order agree on the streak
    recent = rubber_stamp.load_recent_decisions(session)
    fast_streak = rubber_stamp.get_fast_streak(recent)

    recent_decision_ms = []
    for decision in recent:
        recent_decision_ms.append(decision.decision_ms)

    return StatsOut(
        total_orders=count_orders(session),
        auto_captured=count_orders(session, Order.review_state == ReviewState.auto_approved),
        needs_review=count_orders(
            session,
            Order.status == OrderStatus.authorized,
            Order.review_state == ReviewState.needs_review,
        ),
        approved=count_orders(session, Order.review_state == ReviewState.approved),
        voided=count_orders(session, Order.status == OrderStatus.voided),
        median_decision_ms=median_decision_ms(session),
        fast_streak=fast_streak,
        rubber_stamp_active=fast_streak >= rubber_stamp.STREAK_TO_TRIGGER,
        fast_threshold_ms=rubber_stamp.FAST_APPROVAL_MS,
        recent_decision_ms=recent_decision_ms,
    )
