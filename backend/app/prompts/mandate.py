# Prompt + tool for turning a shopper's words into a structured mandate.
# Tune the wording here, then eyeball the results with:
#   python backend/scripts/try_mandates.py

from backend.app.config import CLAUDE_MODEL

MODEL = CLAUDE_MODEL

# extraction is simple work, low effort keeps it fast and cheap.
# raise to "medium" if the date resolution starts slipping
EFFORT = "low"

SYSTEM_PROMPT = """\
You turn a shopper's instructions to their AI shopping agent into a structured purchase \
mandate. A store uses the mandate to check every order the agent places, so it must say \
exactly what the shopper asked for and nothing more.

Always answer by calling the record_mandate tool exactly once. Don't reply with text.

Rules for the fields:
- Only record what the shopper said or clearly implied. If they didn't give a field, set it \
to null. Never guess a color, size, price, store or date.
- item: the thing to buy, in a few words, without color, size, model or date (those have \
their own fields).
- category: a short lowercase product category, only when the item makes it obvious. When the \
item fits one of these store categories, use that exact text: "sneakers", "groceries", \
"electronics", "event tickets", "subscription". Otherwise something short like "footwear", \
"outdoor gear" or "kitchen".
- model: a specific model name or number the shopper named, like "FP-200". Not a brand or a \
product type. null if they didn't name one.
- event_date: for tickets or bookings, the day of the event itself as YYYY-MM-DD, resolved \
against the current date in the message ("this Saturday", "December 5"). This is not when the \
mandate expires. null if there's no event or they didn't say which day.
- quantity: how many units of the item, only if the shopper said ("two packs of batteries" \
is 2, with item "packs of AA batteries"). null otherwise.
- max_price: the most they'll pay, as a number, in whatever currency they said. Don't convert \
it ("under 100 euros" is 100). If they said the limit includes shipping ("all in", "including \
shipping", "total"), use that number and mention it in notes.
- currency: the ISO 4217 code for the currency they named, in uppercase. "$" and "bucks" are \
USD, "euros" and "€" are EUR, "£" is GBP, "C$" is CAD. null if they didn't name a currency, \
like a bare "budget of 100".
- max_price_inclusive: whether a total exactly equal to max_price is allowed. false when the \
shopper wants to stay strictly below it ("under $120", "less than $120", "below $120"). true \
when the number itself is fine ("up to $120", "max $120", "$120 max", "no more than $120", \
"nothing over $120", "a budget of $120"). true when it's unclear or there's no max_price.
- merchant: only if the shopper named a store. Just the name, without trailing punctuation.
- expires_at: when the mandate stops being valid, resolved against the current date and time \
in the message. Use the end of the period they named: "today" is 23:59:59 today, "this week" \
is 23:59:59 on the coming Sunday (today, if today is Sunday), "by Friday" is 23:59:59 on that \
Friday, "this month" is 23:59:59 on the last day of the month. Use UTC unless the shopper gave \
a time zone. Write it as ISO 8601 with an offset, like 2026-10-25T23:59:59+00:00. null if they \
gave no time limit.
- notes: anything else they said that matters for the purchase (preferences, things to avoid, \
conditions). null if there's nothing.
"""

RECORD_MANDATE_TOOL = {
    "name": "record_mandate",
    "description": "Record the shopper's purchase mandate. Use null for anything the shopper "
    "didn't say.",
    # strict: the API guarantees the tool input matches this schema exactly
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "item": {"type": ["string", "null"], "description": "What to buy."},
            "category": {"type": ["string", "null"], "description": "Lowercase category."},
            "color": {"type": ["string", "null"]},
            "size": {"type": ["string", "null"], "description": "Size as the shopper said it."},
            "model": {"type": ["string", "null"], "description": "Model name or number."},
            "event_date": {
                "type": ["string", "null"],
                "description": "Day of the event, YYYY-MM-DD.",
            },
            "quantity": {"type": ["integer", "null"]},
            "max_price": {
                "type": ["number", "null"],
                "description": "Most they'll pay, not converted, total including shipping "
                "if implied.",
            },
            "currency": {
                "type": ["string", "null"],
                "description": "ISO 4217 code of the currency they named, like USD or EUR.",
            },
            "max_price_inclusive": {
                "type": "boolean",
                "description": "false for 'under $X' (total must be below X), true for "
                "'up to $X' / 'max $X' (total may equal X). true if unclear.",
            },
            "merchant": {"type": ["string", "null"]},
            "expires_at": {
                "type": ["string", "null"],
                "description": "ISO 8601 date-time with offset.",
            },
            "notes": {"type": ["string", "null"]},
        },
        # strict mode wants every property listed, nullability carries "not given"
        "required": [
            "item",
            "category",
            "color",
            "size",
            "model",
            "event_date",
            "quantity",
            "max_price",
            "currency",
            "max_price_inclusive",
            "merchant",
            "expires_at",
            "notes",
        ],
        "additionalProperties": False,
    },
}


def build_user_message(request_text, now):
    # the date goes in the user turn, not the system prompt, so the system prompt and
    # tool stay byte-identical across requests (cache friendly)
    return (
        "Current date and time: " + now.isoformat() + " (" + now.strftime("%A") + ")\n\n"
        "Shopper's instructions to their agent:\n"
        "<request>\n" + request_text + "\n</request>"
    )
