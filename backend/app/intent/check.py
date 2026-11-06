# The intent check itself: hard rules first, Claude only if every hard rule passes.
#
# This only answers "does this order fit the mandate?". It never touches PayPal. Capturing or
# voiding based on the answer is the auto-decide step.
#
# Like ai_check.py, this never raises. The money is already authorized by the time it runs, so
# a bug in here has to end with an order waiting for a human, not a failed request.

import logging
import time
from dataclasses import dataclass, field

from backend.app import mandate_parser
from backend.app.config import get_settings
from backend.app.intent.ai_check import (
    FAILED_REASON,
    FAILED_RISK_SCORE,
    SHARP_MAX_WORDS,
    check_order_intent,
    limit_words,
)
from backend.app.intent.rules import (
    RULE_CURRENCY,
    RULE_EXPIRY,
    RULE_MERCHANT,
    RULE_PRICE,
    RuleResult,
    check_hard_rules,
    hard_rules_verdict,
)
from backend.app.models import CheckedBy, Decision, Verdict

logger = logging.getLogger(__name__)

# a failed hard rule isn't a judgment call. the order broke something the shopper said exactly
HARD_RULE_RISK_SCORE = 100

# when several rules fail, the merchant sees the most important one first. the wrong store or a
# dead mandate means the shopper never wanted this at all, a high price might just need a yes
RULE_PRIORITY = [RULE_MERCHANT, RULE_EXPIRY, RULE_CURRENCY, RULE_PRICE]


@dataclass(frozen=True)
class IntentCheckResult:
    verdict: Verdict
    # 0 to 100, higher means more likely the human didn't want this
    risk_score: int
    # one line for a busy merchant
    reason: str
    mismatched_fields: list[str] = field(default_factory=list)
    rule_results: list[RuleResult] = field(default_factory=list)
    checked_by: CheckedBy = CheckedBy.rules
    # the whole check, rules plus Claude if it got that far
    latency_ms: int = 0


def run_intent_check(mandate, order, client=None, now=None, clock=time.monotonic, sharp=False):
    # client, now and clock are for tests and for sharing one Anthropic client across requests.
    # sharp is on while the rubber-stamp watch is: reasons come out as "Mandate said X, order has Y"
    started = clock()
    try:
        return run_checks(mandate, order, client, now, clock, started, sharp)
    except Exception:
        # Claude's own failures are handled inside ai_check.py, so this means the rules or our
        # own code broke. don't carry on to Claude with rules we couldn't run
        logger.exception("Intent check failed for order %s, sending to review", order.id)
        return IntentCheckResult(
            verdict=Verdict.unsure,
            risk_score=FAILED_RISK_SCORE,
            reason=FAILED_REASON,
            checked_by=CheckedBy.rules,
            latency_ms=elapsed_ms(clock, started),
        )


def run_checks(mandate, order, client, now, clock, started, sharp=False):
    rule_results = check_hard_rules(mandate, order, now=now)

    # any failure stops here, which is cheaper and the reason is exact
    if hard_rules_verdict(rule_results) is not None:
        return rejected_by_rules(rule_results, clock, started, sharp)

    if client is None:
        client = mandate_parser.make_client(get_settings().anthropic_api_key)

    ai_result = check_order_intent(client, mandate, order, sharp=sharp)
    return IntentCheckResult(
        verdict=ai_result.verdict,
        risk_score=ai_result.risk_score,
        reason=ai_result.reason,
        mismatched_fields=ai_result.mismatched_fields,
        rule_results=rule_results,
        checked_by=CheckedBy.ai,
        latency_ms=elapsed_ms(clock, started),
    )


def rejected_by_rules(rule_results, clock, started, sharp=False):
    failed_rules = []
    reason_by_rule = {}
    sharp_reason_by_rule = {}
    for result in rule_results:
        if not result.passed:
            failed_rules.append(result.rule)
            reason_by_rule[result.rule] = result.reason
            sharp_reason_by_rule[result.rule] = result.sharp_reason or result.reason

    if sharp:
        # just the top failure, no "(+1 more)": every word counts against the limit, and
        # rule_results still has the full list
        top_rule = leading_rule(failed_rules, reason_by_rule)
        reason = limit_words(sharp_reason_by_rule[top_rule], SHARP_MAX_WORDS)
    else:
        reason = leading_reason(failed_rules, reason_by_rule)

    return IntentCheckResult(
        verdict=Verdict.mismatch,
        risk_score=HARD_RULE_RISK_SCORE,
        reason=reason,
        mismatched_fields=failed_rules,
        rule_results=rule_results,
        checked_by=CheckedBy.rules,
        latency_ms=elapsed_ms(clock, started),
    )


def leading_rule(failed_rules, reason_by_rule):
    # a rule someone adds later without a priority still gets reported, just not first
    for rule in RULE_PRIORITY:
        if rule in reason_by_rule:
            return rule
    return failed_rules[0]


def leading_reason(failed_rules, reason_by_rule):
    # one line: the top failure plus a count of the rest. rule_results has the full list
    reason = reason_by_rule[leading_rule(failed_rules, reason_by_rule)]
    others = len(failed_rules) - 1
    if others > 0:
        reason += " (+" + str(others) + " more)"
    return reason


def elapsed_ms(clock, started):
    return int((clock() - started) * 1000)


def build_decision(order, result):
    # the row for the decisions table, holding the full result
    rule_results = []
    for rule_result in result.rule_results:
        rule_results.append({
            "rule": rule_result.rule,
            "passed": rule_result.passed,
            "reason": rule_result.reason,
        })

    return Decision(
        order=order,
        verdict=result.verdict,
        risk_score=result.risk_score,
        reason=result.reason,
        mismatched_fields=result.mismatched_fields,
        rule_results=rule_results,
        checked_by=result.checked_by,
        latency_ms=result.latency_ms,
    )
