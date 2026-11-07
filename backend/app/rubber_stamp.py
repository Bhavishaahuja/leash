# The rubber-stamp watch. Escalating every doubtful order is pointless if the human just taps
# approve without reading. When they approve escalated orders too fast, too many times in a row,
# Leash escalates less often, and what it does escalate comes with a sharper reason.
#
# Only human decisions count: an approval row with an action. Those only exist for orders that
# were escalated, since auto-captures never get one. decision_ms is the server's own number
# (opened to tap), so a client can't fake attention.
#
# The watch is global, not per shopper. One merchant reviews everything in this demo.

from sqlalchemy import select

from backend.app.config import get_settings
from backend.app.intent.ai_check import FAILED_REASON, TIMED_OUT_REASON
from backend.app.models import Approval

FAST_APPROVAL_MS = 2000   # under this, they almost certainly didn't read it
STREAK_TO_TRIGGER = 3     # this many fast approvals in a row turns the watch on
LOOKBACK = 10             # how many recent human decisions we look at

# what the dashboard shows for an order the watch let through, in place of Claude's reason
LET_THROUGH_REASON = "Let through: low risk while rubber-stamp watch was on"

# code-set reasons for a check that never really ran. their risk score is a placeholder
CHECK_DID_NOT_RUN_REASONS = [FAILED_REASON, TIMED_OUT_REASON]


def get_fast_streak(recent_decisions):
    # newest first, only human decisions on escalated orders (auto-captures don't count)
    streak = 0
    for decision in recent_decisions:
        if decision.action != "approve":
            break  # a reject means they're actually paying attention, streak over
        if decision.decision_ms is None or decision.decision_ms >= FAST_APPROVAL_MS:
            break  # a slow, careful approval also resets it
        streak += 1
    return streak


def should_escalate(check, watch_active):
    # these always go to the human, no matter what
    if check.hard_rule_failed:
        return True
    if check.verdict == "mismatch":
        return True
    # added: Claude errored or timed out, so the 50 on it is a placeholder, not a judgment.
    # without this, sharp mode would capture orders nobody actually checked
    if check.reason in CHECK_DID_NOT_RUN_REASONS:
        return True
    # added: rows from before the check was wired in can have no score, and None >= 60 crashes
    if check.risk_score is None:
        return True

    if watch_active:
        # human is rubber-stamping, so only interrupt them for the risky stuff
        return check.risk_score >= 60

    # normal mode: keep whatever the current auto-decide logic already does. that's a match
    # under AUTO_CAPTURE_MAX_RISK, so a match at or over it still goes to a human
    if check.verdict != "match":
        return True
    return check.risk_score >= get_settings().auto_capture_max_risk


def load_recent_decisions(session):
    # newest first. ties on decided_at go to the later row
    query = (
        select(Approval)
        .where(Approval.action.is_not(None))
        .order_by(Approval.decided_at.desc(), Approval.id.desc())
        .limit(LOOKBACK)
    )
    return list(session.scalars(query).all())


def is_watch_active(recent_decisions):
    return get_fast_streak(recent_decisions) >= STREAK_TO_TRIGGER


def watch_is_on(session):
    return is_watch_active(load_recent_decisions(session))


def let_through_by_watch(check, watch_active):
    # True when the order only skipped the human because the watch was on. normal mode would
    # have escalated it, so the record has to say why it didn't
    if not watch_active:
        return False
    return should_escalate(check, False) and not should_escalate(check, True)
