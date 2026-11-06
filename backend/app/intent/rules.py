# The hard rules: deterministic mandate checks that run before Claude.
#
# If any of these fails the verdict is "mismatch" and Claude never gets called. That's cheaper,
# and the reason is exact ("Order is $142.00, mandate cap is $120.00"), not a judgment call.
# All rules passing doesn't mean the order is a match: item, color and size are still Claude's
# job, so hard_rules_verdict returns None in that case.

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal

from backend.app.config import STORE_ALIASES, STORE_NAME
from backend.app.models import Verdict

CENT = Decimal("0.01")
# a mandate that names no currency means dollars, see check_price
DEFAULT_CURRENCY = "USD"

RULE_PRICE = "price"
RULE_MERCHANT = "merchant"
RULE_EXPIRY = "expiry"
RULE_CURRENCY = "currency"


@dataclass(frozen=True)
class RuleResult:
    rule: str
    passed: bool
    # one line for the human, like "Order is $142.00, mandate cap is $120.00"
    reason: str


def check_hard_rules(mandate, order, now=None):
    # always runs all four, so the record shows everything that was wrong and not just the first
    if now is None:
        now = datetime.now(timezone.utc)

    results = []
    results.append(check_price(mandate, order))
    results.append(check_merchant(mandate, order.merchant))
    results.append(check_expiry(mandate, now))
    results.append(check_currency(mandate, order))
    return results


def hard_rules_verdict(results):
    # Verdict.mismatch if anything failed. None means "no hard failure, hand it to Claude"
    for result in results:
        if not result.passed:
            return Verdict.mismatch
    return None


# ---- the rules ----


def check_price(mandate, order):
    if mandate.max_price is None:
        return RuleResult(RULE_PRICE, True, "Mandate sets no price cap")

    # comparing 100 euros to 100 dollars would be meaningless, so don't try. this counts as
    # a pass because check_currency is the rule that reports the problem
    cap_currency = mandate_currency(mandate) or DEFAULT_CURRENCY
    order_currency = clean_currency(order.currency)
    if cap_currency != order_currency:
        return RuleResult(RULE_PRICE, True, "Skipped, cap is in " + cap_currency)

    cap = as_decimal(mandate.max_price)
    total = as_decimal(order.total)
    cap_text = format_money(cap, order_currency)
    order_text = describe_order_total(order, total, order_currency)

    # only an explicit False means "under $X". the column default is True
    if mandate.max_price_inclusive is False:
        if total < cap:
            return RuleResult(RULE_PRICE, True, order_text + ", under the " + cap_text + " cap")
        return RuleResult(RULE_PRICE, False, order_text + ", mandate requires under " + cap_text)

    if total <= cap:
        return RuleResult(RULE_PRICE, True, order_text + ", within the " + cap_text + " cap")
    return RuleResult(RULE_PRICE, False, order_text + ", mandate cap is " + cap_text)


def check_merchant(mandate, order_merchant=None):
    wanted = normalize_merchant(mandate.merchant)
    if not wanted:
        return RuleResult(RULE_MERCHANT, True, "Mandate names no store")

    # no merchant on the order means our one store, which is every real agent order
    order_store = order_merchant or STORE_NAME
    accepted_names = [order_store]
    if normalize_merchant(order_store) == normalize_merchant(STORE_NAME):
        for alias in STORE_ALIASES:
            accepted_names.append(alias)

    for name in accepted_names:
        if wanted == normalize_merchant(name):
            return RuleResult(RULE_MERCHANT, True, "Mandate store matches " + order_store)

    return RuleResult(
        RULE_MERCHANT,
        False,
        "Mandate says " + mandate.merchant.strip() + ", order is at " + order_store,
    )


def check_expiry(mandate, now):
    if mandate.expires_at is None:
        return RuleResult(RULE_EXPIRY, True, "Mandate has no expiry")

    expires_at = as_utc(mandate.expires_at)
    expires_text = expires_at.strftime("%Y-%m-%d %H:%M") + " UTC"

    # still good at the exact expiry moment, it's only expired once that has gone by
    if as_utc(now) > expires_at:
        return RuleResult(RULE_EXPIRY, False, "Mandate expired " + expires_text)
    return RuleResult(RULE_EXPIRY, True, "Mandate is valid until " + expires_text)


def check_currency(mandate, order):
    wanted = mandate_currency(mandate)
    order_currency = clean_currency(order.currency)

    if wanted is None:
        return RuleResult(RULE_CURRENCY, True, "Mandate names no currency")
    if wanted != order_currency:
        return RuleResult(
            RULE_CURRENCY, False, "Mandate is in " + wanted + ", order is in " + order_currency
        )
    return RuleResult(RULE_CURRENCY, True, "Both are in " + order_currency)


# ---- helpers ----


def mandate_currency(mandate):
    # lives in the rules JSON, not a column. mandates parsed before it existed just don't have it
    return clean_currency(mandate.rules.get("currency"))


def clean_currency(value):
    if not value:
        return None
    return value.strip().upper() or None


def normalize_merchant(name):
    # "  FERNWOOD   Supply Co. " and "fernwood supply co" are the same store
    if not name:
        return ""
    words = name.casefold().split()
    return " ".join(words).rstrip(".,;:!")


def as_decimal(value):
    # money is Decimal from the Numeric columns already. going via str means a stray float
    # can't sneak 119.98999999 into the comparison
    return Decimal(str(value))


def as_utc(moment):
    # Postgres timestamptz always comes back timezone-aware. only SQLite (tests) drops the
    # zone, and what it stored was UTC, so this branch shouldn't run in prod
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def format_money(amount, currency):
    text = str(amount.quantize(CENT, rounding=ROUND_HALF_UP))
    if currency == "USD":
        return "$" + text
    return text + " " + currency


def describe_order_total(order, total, currency):
    # "Order is $125.00 ($115.00 item + $10.00 shipping)"
    # no breakdown when there's no shipping, or on older orders where it wasn't saved
    total_text = "Order is " + format_money(total, currency)
    if order.shipping_amount is None:
        return total_text

    shipping = as_decimal(order.shipping_amount)
    if shipping <= 0:
        return total_text

    goods = total - shipping
    return (
        total_text + " (" + format_money(goods, currency) + " " + goods_label(order)
        + " + " + format_money(shipping, currency) + " shipping)"
    )


def goods_label(order):
    unit_count = 0
    for item in order.items:
        unit_count += item.get("quantity", 1)
    if unit_count == 1:
        return "item"
    return "items"
