# The demo store's catalog: the products Fernwood Supply Co sells, one row per variant.
# The rows themselves come from the migration (b91d6e2f4a73), this is how the app reads them.
#
# The demo scenarios use it to pick a believable wrong variant: a size the store really stocks,
# the other earbud model at the same price, the same event on another day. Picking from what's
# for sale is what makes the wrong order look like something a real agent would do.

import re
from datetime import date

from sqlalchemy import select

from backend.app.models import CatalogItem

# the parser is told to use exactly these as a mandate's category when the request fits one,
# which is how a mandate finds its rows here. see prompts/mandate.py
CATEGORIES = ["sneakers", "groceries", "electronics", "event tickets", "subscription"]

NUMBER_IN_SIZE = re.compile(r"\d+(?:\.\d+)?")


def normalize_category(category):
    if not category:
        return None
    return " ".join(category.casefold().split())


def load_category(session, category):
    # every row in the mandate's category, in a fixed order so the same mandate always gets the
    # same wrong variant. empty when the category isn't one the store sells
    wanted = normalize_category(category)
    if wanted not in CATEGORIES:
        return []
    query = select(CatalogItem).where(CatalogItem.category == wanted).order_by(CatalogItem.id)
    return list(session.scalars(query).all())


# ---- picking a sibling variant ----
# these take plain rows and return a value or None, no database. demo.py says what None means


def other_size(rows, wanted_size):
    # the stocked size closest to the one asked for: 9 gets 8.5, not 8. closer is sneakier.
    # keeps whatever came with the number, so "men's 9" turns into "men's 8.5"
    wanted_number = size_number(wanted_size)
    if wanted_number is None:
        return None

    best_size = None
    best_gap = None
    for row in rows:
        number = size_number(row.size)
        if number is None or number == wanted_number:
            continue
        gap = abs(number - wanted_number)
        if best_gap is None or gap < best_gap:
            best_gap = gap
            best_size = row.size
    if best_size is None:
        return None
    return NUMBER_IN_SIZE.sub(best_size, wanted_size, count=1)


def size_number(size):
    if not size:
        return None
    found = NUMBER_IN_SIZE.search(size)
    if found is None:
        return None
    return float(found.group(0))


def substitute_for(rows, wanted_item):
    # something else in the same category that's actually in stock, which is what an agent
    # reaches for when the real thing is sold out
    for row in rows:
        if row.in_stock and not same_product(row.name, wanted_item):
            return row
    return None


def same_product(catalog_name, wanted_item):
    # same product when every word of the shorter name is in the longer one. "oat milk" and
    # "canvas sneakers" are "Organic oat milk" and "Canvas low-top sneaker". "organic oat milk"
    # isn't "Organic almond milk", oat is missing. a plain substring check got the sneakers wrong
    # and "substituted" the very thing the shopper asked for
    catalog_words = product_words(catalog_name)
    wanted_words = product_words(wanted_item)
    if not catalog_words or not wanted_words:
        return False
    return catalog_words <= wanted_words or wanted_words <= catalog_words


def product_words(name):
    # lowercase words, singular, without the punctuation around them
    words = set()
    for word in re.findall(r"[a-z0-9][a-z0-9'-]*", name.casefold()):
        # "sneakers" and "sneaker" are the same word here. "glass" stays "glass"
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        words.add(word)
    return words


def other_model(rows, wanted_model):
    # another model at the same price as the one asked for, so the total gives nothing away.
    # if the store doesn't sell the one asked for, any other model will do
    wanted = wanted_model.strip().casefold()
    wanted_price = None
    for row in rows:
        if row.model and row.model.casefold() == wanted:
            wanted_price = row.price

    fallback = None
    for row in rows:
        if not row.model or row.model.casefold() == wanted:
            continue
        if wanted_price is None or row.price == wanted_price:
            return row.model
        if fallback is None:
            fallback = row.model
    return fallback


def other_event_date(rows, wanted_date):
    # the same event on the nearest other day the store has tickets for
    best_date = None
    for row in rows:
        if row.event_date is None or row.event_date == wanted_date:
            continue
        gap = abs((row.event_date - wanted_date).days)
        if best_date is None or gap < abs((best_date - wanted_date).days):
            best_date = row.event_date
    return best_date


def format_event_date(event_date):
    # "Dec 5, 2026". built by hand because %-d doesn't exist on Windows. lives here so fixes.py
    # and orders.py both get it without importing each other
    return event_date.strftime("%b") + " " + str(event_date.day) + ", " + str(event_date.year)


def parse_event_date(value):
    # the parser writes "2026-12-05". anything else isn't a date we can move by a day
    if isinstance(value, date):
        return value
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value.strip()[:10])
    except ValueError:
        return None
