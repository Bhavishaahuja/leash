# The fix suggestion: when an order goes to a human, look for what the agent should have bought.
#
# Plain Python over the catalog, never Claude. A suggestion is shown to the human deciding about
# money, so it has to be something we can stand behind: a real SKU that's in stock at the same
# store, matches every field the shopper named, and keeps the total inside their cap. If nothing
# fits, there's no suggestion, and that's a fine answer.
#
# It only suggests. Nothing here changes the order or moves money.

from decimal import Decimal

from backend.app import catalog
from backend.app.config import STORE_NAME
from backend.app.intent.rules import normalize_merchant

# mismatched_fields values a catalog item can fix, mapped to the kind of fix. anything else, like
# price, merchant, expiry or instructions_in_order, gets no suggestion: the store's catalog has
# nothing to say about a dead mandate or an order that tried to instruct the checker
FIXABLE_FIELDS = {
    "size": "size",
    "color": "color",
    "model": "model",
    "event_date": "event_date",
    "date": "event_date",
    "substitution": "item",
    "item": "item",
    "quantity": "quantity",
}

CENT = Decimal("0.01")


def find_fix(session, mandate, order, mismatched_fields):
    # returns {"sku", "item", "price", "quantity", "sentence"}, or None when there's nothing to
    # suggest. price is the catalog's unit price as a string like "64.00", never the order's
    if not order.items:
        return None
    ordered = order.items[0]
    rules = mandate.rules or {}

    kind = fix_kind(mismatched_fields, ordered, rules)
    if kind is None:
        return None

    candidates = []
    for row in catalog.load_category(session, rules.get("category")):
        if row_fits(row, mandate, order, rules):
            candidates.append(row)
    if not candidates:
        return None

    # closest to what the agent was paying, so the suggestion changes as little as possible.
    # ties go to the cheaper one, then the first listed
    original_price = Decimal(str(ordered.get("unit_price") or "0"))

    def closeness(row):
        return (abs(row.price - original_price), row.price, row.id)

    best = min(candidates, key=closeness)
    quantity = wanted_quantity(rules)
    return {
        "sku": best.sku,
        "item": best.name,
        "price": str(best.price.quantize(CENT)),
        "quantity": quantity,
        "sentence": build_sentence(kind, ordered, rules, best, quantity, original_price),
    }


def fix_kind(mismatched_fields, ordered, rules):
    # which field the suggestion is about: the first fixable one Claude named
    for field in mismatched_fields or []:
        kind = FIXABLE_FIELDS.get(field)
        if kind is not None:
            return kind
    # the price rule catches "12 of them at full price" before Claude ever sees the order, so it
    # only says price. if the count is what's off, that's a quantity fix at the catalog's price
    if "price" in (mismatched_fields or []):
        if ordered.get("quantity") != wanted_quantity(rules):
            return "quantity"
    return None


def wanted_quantity(rules):
    # the parser's own fallback: a shopper who didn't say how many wants one
    return rules.get("quantity") or 1


# ---- does this catalog row fit the mandate ----


def row_fits(row, mandate, order, rules):
    if not row.in_stock:
        return False
    if not same_store(row, order):
        return False
    # the thing the shopper asked for, not whatever else is on the shelf. otherwise a
    # substitution could get "fixed" with another substitution
    item = rules.get("item")
    if item and not catalog.same_product(row.name, item):
        return False
    # every variant field the shopper named has to match. one they left open matches anything
    if rules.get("size") and not same_size(row.size, rules["size"]):
        return False
    if rules.get("color") and not same_text(row.color, rules["color"]):
        return False
    if rules.get("model") and not same_text(row.model, rules["model"]):
        return False
    if rules.get("event_date"):
        wanted_date = catalog.parse_event_date(rules["event_date"])
        if wanted_date is None or row.event_date != wanted_date:
            return False
    return within_cap(row, mandate, order, rules)


def same_store(row, order):
    # no merchant on the order means our store, same as the merchant rule
    return normalize_merchant(row.merchant) == normalize_merchant(order.merchant or STORE_NAME)


def same_text(catalog_value, wanted):
    if not catalog_value:
        return False
    return " ".join(catalog_value.casefold().split()) == " ".join(wanted.casefold().split())


def same_size(catalog_size, wanted):
    # "men's 9" and "size 9" are both the catalog's "9". without numbers, the words have to match
    if not catalog_size:
        return False
    catalog_number = catalog.size_number(catalog_size)
    wanted_number = catalog.size_number(wanted)
    if catalog_number is not None and wanted_number is not None:
        return catalog_number == wanted_number
    return same_text(catalog_size, wanted)


def within_cap(row, mandate, order, rules):
    # the total the price rule would see if the agent placed this instead: the catalog price for
    # the quantity the shopper asked for, plus the same shipping. a suggestion that would fail the
    # rule is no suggestion at all
    if mandate.max_price is None:
        return True
    # the catalog is in dollars. comparing to a cap in another currency would mean nothing
    currency = (rules.get("currency") or "USD").strip().upper()
    if currency != "USD":
        return False

    shipping = Decimal(str(order.shipping_amount or "0"))
    total = row.price * wanted_quantity(rules) + shipping
    cap = Decimal(str(mandate.max_price))
    # only an explicit False means "under $X", same as the price rule
    if mandate.max_price_inclusive is False:
        return total < cap
    return total <= cap


# ---- the sentence ----


def build_sentence(kind, ordered, rules, row, quantity, original_price):
    # "Agent picked size 8.5, mandate says 9. Size 9 is in stock at the same price."
    price = price_phrase(row.price, original_price, quantity)

    if kind == "size":
        return ("Agent picked " + size_label(ordered.get("size")) + ", mandate says "
                + bare_size(rules["size"]) + ". " + capitalize(size_label(row.size))
                + " is in stock " + price + ".")
    if kind == "color":
        return ("Agent picked " + (ordered.get("color") or "no color") + ", mandate says "
                + rules["color"] + ". " + capitalize(row.color) + " is in stock " + price + ".")
    if kind == "model":
        return ("Agent picked model " + (ordered.get("model") or "none") + ", mandate says "
                + rules["model"] + ". Model " + row.model + " is in stock " + price + ".")
    if kind == "event_date":
        return ("Agent picked " + date_label(ordered.get("event_date")) + ", mandate says "
                + date_label(rules["event_date"]) + ". " + row.name + " for "
                + catalog.format_event_date(row.event_date) + " is in stock " + price + ".")
    if kind == "item":
        return ("Agent picked " + ordered.get("name", "something else") + ", mandate says "
                + rules.get("item", "something else") + ". " + row.name + " is in stock "
                + price + ".")

    # quantity: the fix is the same thing, just the number the shopper asked for
    ordered_count = ordered.get("quantity") or 1
    sentence = ("Agent picked " + count_label(ordered_count, row.quantity_unit) + ", mandate says "
                + str(quantity) + ". " + row.name + " is in stock at " + dollars(row.price))
    if quantity > 1:
        sentence += " each, " + dollars(row.price * quantity) + " for " + str(quantity)
    return sentence + "."


def price_phrase(catalog_price, original_price, quantity):
    if catalog_price == original_price:
        return "at the same price"
    if quantity > 1:
        return "at " + dollars(catalog_price) + " each"
    return "at " + dollars(catalog_price)


def dollars(amount):
    return "$" + str(Decimal(str(amount)).quantize(CENT))


def bare_size(size):
    # "size 9" -> "9", so "mandate says 9" doesn't read "mandate says size 9" after "size 8.5"
    text = (size or "").strip()
    if text.casefold().startswith("size "):
        return text[len("size "):].strip()
    return text


def size_label(size):
    # "8.5" -> "size 8.5". "men's size 8.5" already says size, so it stays as it is, or it would
    # come out "size men's size 8.5"
    if not size:
        return "no size"
    if "size" in size.casefold().split():
        return size.strip()
    return "size " + bare_size(size)


def date_label(value):
    parsed = catalog.parse_event_date(value)
    if parsed is None:
        return str(value or "no date")
    return catalog.format_event_date(parsed)


def count_label(count, unit):
    # "2 months", "1 pair". the catalog's units are all regular plurals
    if count == 1:
        return "1 " + unit
    return str(count) + " " + unit + "s"


def capitalize(text):
    if not text:
        return ""
    return text[0].upper() + text[1:]
