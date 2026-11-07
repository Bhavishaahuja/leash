# The fuzzy half of the intent check: wrong color, wrong variant, odd quantity, substitutions.
# Only runs once the hard rules in rules.py have passed.
#
# Claude Sonnet 5.5 can't do two things you'd normally reach for here:
#   - forced tool_choice ("any" / "tool") is a 400, so this is tool_choice auto + a prompt that
#     says to always call the tool + strict: true, and we check for a call and retry once
#   - temperature isn't accepted at all, the SDK raises TypeError on it. so the same order can
#     get a slightly different answer between runs, which is why this file validates everything
#
# This function never raises. Anything going wrong ends in "unsure", which sends the order to a
# human, and a match is the only result that can ever capture money.

import logging
import time
from dataclasses import dataclass, field

from backend.app.models import Verdict
from backend.app.prompts import intent as intent_prompt

logger = logging.getLogger(__name__)

# server-side fallback: if Sonnet 5.5 declines, fallbacks="default" retries on another model
# inside the same call. same setup as mandate_parser.py
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# auto doesn't guarantee a tool call, so we get one more try
MAX_ATTEMPTS = 2

# this runs while someone waits on POST /orders, and the SDK default is 10 minutes with 2 retries.
# a 429 or 529 goes straight to a human too, a late answer is worth less than a quick review
ATTEMPT_TIMEOUT_SECONDS = 10
SDK_RETRIES = 0

# the SDK timeout is per network step (connect, send, read), not one stopwatch over the whole
# call, so a single attempt can still run past 10s. these are measured across the whole check
OVERALL_BUDGET_SECONDS = 20
# a retry started later than this wouldn't have a useful amount of budget left
RETRY_CUTOFF_SECONDS = 12

MIN_RISK_SCORE = 0
MAX_RISK_SCORE = 100
# the prompt asks for under 90, this is the backstop for when it runs long
MAX_REASON_LENGTH = 119
TRIM_MARKER = "..."
# with the rubber-stamp watch on, reasons have to be under 12 words. the prompt asks, this checks
SHARP_MAX_WORDS = 11

FAILED_RISK_SCORE = 50
FAILED_REASON = "Couldn't verify automatically, please review"
TIMED_OUT_REASON = "Check timed out, please review"

# set in code, not left to the model: the merchant always gets the same clear wording, and
# whatever the order said can't get repeated back to them
INSTRUCTIONS_FIELD = "instructions_in_order"
INSTRUCTIONS_REASON = "Order text tried to instruct the checker"


class AICheckError(Exception):
    # Claude answered, but not with anything we can trust
    pass


class CheckOverBudget(Exception):
    # out of time for the whole check
    pass


@dataclass(frozen=True)
class AICheckResult:
    verdict: Verdict
    # 0 to 100, higher means more likely the human didn't want this
    risk_score: int
    # one line for a busy merchant
    reason: str
    mismatched_fields: list[str] = field(default_factory=list)


def check_order_intent(client, mandate, order, clock=time.monotonic, sharp=False):
    # clock is only passed in by tests, so they don't have to really wait 20 seconds.
    # sharp is the rubber-stamp watch: same call, it just asks for a shorter, pointed reason
    started = clock()

    # the broad except is on purpose. a missing API key shows up as a TypeError from the SDK, not
    # an APIError, and no failure of any kind should stop an order reaching a human.
    # the log is what keeps a real bug from just looking like a lot of "unsure" orders
    try:
        result = run_check(client, mandate, order, clock, started, sharp)
    except CheckOverBudget:
        logger.warning("AI check for order %s ran out of time, sending to review", order.id)
        return timed_out_result()
    except Exception:
        logger.exception("AI check failed for order %s, sending to review", order.id)
        # a retry that times out with the budget spent is a budget problem, not an API one
        if clock() - started >= OVERALL_BUDGET_SECONDS:
            return timed_out_result()
        return AICheckResult(Verdict.unsure, FAILED_RISK_SCORE, FAILED_REASON, [])

    # over budget is over budget, even when the answer itself is fine
    if clock() - started > OVERALL_BUDGET_SECONDS:
        logger.warning("AI check for order %s answered too late, sending to review", order.id)
        return timed_out_result()
    return result


def timed_out_result():
    return AICheckResult(Verdict.unsure, FAILED_RISK_SCORE, TIMED_OUT_REASON, [])


def run_check(client, mandate, order, clock, started, sharp=False):
    user_message = intent_prompt.build_user_message(mandate, order, sharp=sharp)

    for attempt in range(MAX_ATTEMPTS):
        elapsed = clock() - started
        if attempt > 0 and elapsed > RETRY_CUTOFF_SECONDS:
            raise CheckOverBudget()

        # a retry only gets what's left of the budget, not a fresh 10 seconds
        timeout = min(ATTEMPT_TIMEOUT_SECONDS, OVERALL_BUDGET_SECONDS - elapsed)
        response = call_claude(client, user_message, timeout)

        # always check stop_reason before trusting content
        if response.stop_reason == "refusal":
            raise AICheckError("Claude declined to check this order")

        tool_input = find_tool_input(response)
        if tool_input is not None:
            return validate_output(tool_input, sharp)
        # no finished tool call this time, go around once more

    raise AICheckError("Claude didn't call the tool after " + str(MAX_ATTEMPTS) + " tries")


def call_claude(client, user_message, timeout):
    limited_client = client.with_options(timeout=timeout, max_retries=SDK_RETRIES)
    return limited_client.beta.messages.create(
        model=intent_prompt.MODEL,
        max_tokens=16000,
        system=intent_prompt.SYSTEM_PROMPT,
        tools=[intent_prompt.RECORD_INTENT_CHECK_TOOL],
        # one call only, so there's never a second verdict to choose between
        tool_choice={"type": "auto", "disable_parallel_tool_use": True},
        output_config={"effort": intent_prompt.EFFORT},
        betas=[FALLBACK_BETA],
        fallbacks="default",
        messages=[{"role": "user", "content": user_message}],
    )


def find_tool_input(response):
    # a max_tokens stop can leave a half-written tool call behind, so it has to be a clean finish
    if response.stop_reason != "tool_use":
        return None
    tool_name = intent_prompt.RECORD_INTENT_CHECK_TOOL["name"]
    for block in response.content:
        if block.type == "tool_use" and block.name == tool_name:
            return block.input
    return None


# ---- validating what came back ----


def validate_output(tool_input, sharp=False):
    verdict = clean_verdict(tool_input.get("verdict"))
    risk_score = clean_risk_score(tool_input.get("risk_score"))
    reason = clean_reason(tool_input.get("reason"))
    if sharp:
        reason = limit_words(reason, SHARP_MAX_WORDS)
    mismatched_fields = clean_mismatched_fields(tool_input.get("mismatched_fields"))

    # a match captures money, so it doesn't get the benefit of the doubt if it contradicts itself
    if verdict == Verdict.match and mismatched_fields:
        raise AICheckError("Verdict is match but mismatched_fields isn't empty")

    if INSTRUCTIONS_FIELD in mismatched_fields:
        reason = INSTRUCTIONS_REASON

    return AICheckResult(verdict, risk_score, reason, mismatched_fields)


def clean_verdict(value):
    try:
        return Verdict(value)
    except ValueError as error:
        raise AICheckError("Unknown verdict: " + repr(value)) from error


def clean_risk_score(value):
    # bool counts as an int in Python, and True isn't a score
    if isinstance(value, bool) or not isinstance(value, int):
        raise AICheckError("risk_score isn't an integer: " + repr(value))
    if value < MIN_RISK_SCORE:
        return MIN_RISK_SCORE
    if value > MAX_RISK_SCORE:
        return MAX_RISK_SCORE
    return value


def clean_reason(value):
    if not isinstance(value, str):
        raise AICheckError("reason isn't a string: " + repr(value))

    # one line, however it arrived
    text = " ".join(value.split())
    if not text:
        raise AICheckError("reason is blank")

    # the prompt asks for no em dashes, this is for when it slips. it's UI copy
    text = text.replace(" \N{EM DASH} ", ", ").replace("\N{EM DASH}", ", ")

    # a long reason isn't worth throwing away a good verdict, so trim it
    if len(text) > MAX_REASON_LENGTH:
        return trim_at_word(text)
    return text


def trim_at_word(text):
    keep = MAX_REASON_LENGTH - len(TRIM_MARKER)
    trimmed = text[:keep]

    # if the cut landed inside a word, back up to the space before it. one giant word with no
    # spaces has nowhere to back up to, so that just gets cut
    if text[keep] != " " and " " in trimmed:
        trimmed = trimmed.rsplit(" ", 1)[0]

    return trimmed.rstrip(" ,;:.") + TRIM_MARKER


def limit_words(text, max_words):
    # a sharp reason that runs long still names the field up front, so keep the start of it
    words = text.split()
    if len(words) <= max_words:
        return text
    kept = []
    for word in words[:max_words]:
        kept.append(word)
    return " ".join(kept).rstrip(" ,;:.") + TRIM_MARKER


def clean_mismatched_fields(value):
    if not isinstance(value, list):
        raise AICheckError("mismatched_fields isn't a list: " + repr(value))

    fields = []
    for item in value:
        if not isinstance(item, str):
            raise AICheckError("mismatched_fields has a non-string: " + repr(item))
        if item.strip():
            fields.append(item.strip())
    return fields
