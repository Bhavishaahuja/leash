# Demo support. Two separate things live here:
#   POST /demo/orders builds an order from a mandate's rules, right or wrong on purpose, and sends
#   it down the same path as an agent's order. It's what the /place-order page calls.
#   DELETE /demo/seeded supports backend/scripts/seed_demo.py, which has to work against Render with
#   only a base URL. Seeded rows are the ones whose mandate has a buyer_label starting with
#   SEED_LABEL_PREFIX.

import enum
from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy import delete, select

from backend.app import catalog
from backend.app.auth import require_merchant
from backend.app.config import STORE_NAME, get_settings
from backend.app.intent.rules import normalize_merchant
from backend.app.models import Approval, Decision, Dispute, Mandate, Order, OrderStatus
from backend.app.orders import (
    CENT,
    Claude,
    DbSession,
    OrderDetail,
    OrderItemIn,
    PayPal,
    load_order,
    place_order,
)
from backend.app.review import hold_has_expired

# the part after the colon names the scenario, which is how a re-run finds what's already there
SEED_LABEL_PREFIX = "seed-demo:"

CLEAN_SHARE_OF_CAP = Decimal("0.80")
OVER_CAP_SHARE_OF_CAP = Decimal("1.15")
# a fixed, ordinary price for a mandate with no cap, where 80% of it means nothing. the shopper set
# no limit, so any price passes the rule and this number doesn't affect the verdict
PRICE_WITHOUT_CAP = Decimal("50.00")
# shipping stays out of it, so the percentages above are exactly what the price rule compares
DEMO_SHIPPING = Decimal("0.00")

# the other store from the seed data, for the wrong_store scenario
OTHER_STORE_NAME = "Ridgeline Outdoors"
# first one that doesn't appear in the mandate's color wins
WRONG_COLORS = ["red", "green", "orange"]

# who a /place-order order is credited to. the merchant placed it, standing in for an agent, so it
# gets its own name instead of borrowing a real agent's
DEMO_AGENT_NAME = "Demo page"

router = APIRouter(prefix="/demo", tags=["demo"])


class DemoScenario(str, enum.Enum):
    clean = "clean"
    wrong_color = "wrong_color"
    over_cap = "over_cap"
    wrong_store = "wrong_store"
    # these five pick their wrong variant from the catalog, see catalog.py
    wrong_size = "wrong_size"
    substitution = "substitution"
    wrong_model = "wrong_model"
    wrong_date = "wrong_date"
    # no catalog needed: one more unit for the same money
    wrong_quantity = "wrong_quantity"


class DemoOrderCreate(BaseModel):
    mandate_id: int
    scenario: DemoScenario


@dataclass(frozen=True)
class DemoOrder:
    items: list[OrderItemIn]
    shipping: Decimal
    merchant: str | None


class ScenarioNotPossible(Exception):
    # this mandate has nothing for the scenario to break. a "wrong color" on a mandate that names
    # no color is a perfectly fine order, and it would auto-capture while pretending to be a test
    pass


def build_demo_order(mandate, scenario, catalog_rows=None):
    # no database and no PayPal in here, just the mandate's rules turned into an order. each
    # scenario starts from the same matching order and breaks exactly one thing.
    # catalog_rows is the catalog for the mandate's category (catalog.load_category), the
    # scenarios that need a real sibling variant pick it from there
    if catalog_rows is None:
        catalog_rows = []
    rules = mandate.rules
    # checked first, every scenario needs it. a placeholder name can't be checked against anything,
    # so Claude says unsure and even a clean order can never auto-capture
    item_name = product_name(rules.get("item"))
    if item_name is None:
        raise ScenarioNotPossible("This mandate doesn't name an item to order")

    color = rules.get("color")
    size = rules.get("size")
    model = rules.get("model")
    event_date = catalog.parse_event_date(rules.get("event_date"))
    # same fallback as the parser: a shopper who didn't say how many wants one
    quantity = rules.get("quantity") or 1
    merchant = mandate.merchant
    target_total = clean_total(mandate)
    rounding = ROUND_DOWN

    if scenario == DemoScenario.wrong_color:
        if not color:
            raise ScenarioNotPossible("This mandate doesn't name a color to get wrong")
        color = different_color(color)
    elif scenario == DemoScenario.over_cap:
        if mandate.max_price is None:
            raise ScenarioNotPossible("This mandate has no price cap to go over")
        target_total = Decimal(str(mandate.max_price)) * OVER_CAP_SHARE_OF_CAP
        # up, so a small cap split across several units still lands over it
        rounding = ROUND_UP
    elif scenario == DemoScenario.wrong_store:
        if not merchant:
            raise ScenarioNotPossible("This mandate doesn't name a store, so any store is fine")
        merchant = different_store(merchant)
    elif scenario == DemoScenario.wrong_size:
        if not size:
            raise ScenarioNotPossible("This mandate doesn't name a size to get wrong")
        require_catalog(catalog_rows)
        size = catalog.other_size(catalog_rows, size)
        if size is None:
            raise ScenarioNotPossible("The catalog has no other size of this")
    elif scenario == DemoScenario.substitution:
        require_catalog(catalog_rows)
        substitute = catalog.substitute_for(catalog_rows, item_name)
        if substitute is None:
            raise ScenarioNotPossible("The catalog has nothing else in this category to swap in")
        item_name = substitute.name
    elif scenario == DemoScenario.wrong_model:
        if not model:
            raise ScenarioNotPossible("This mandate doesn't name a model to get wrong")
        require_catalog(catalog_rows)
        model = catalog.other_model(catalog_rows, model)
        if model is None:
            raise ScenarioNotPossible("The catalog has no other model of this")
    elif scenario == DemoScenario.wrong_date:
        if event_date is None:
            raise ScenarioNotPossible("This mandate doesn't name an event date to get wrong")
        require_catalog(catalog_rows)
        event_date = catalog.other_event_date(catalog_rows, event_date)
        if event_date is None:
            raise ScenarioNotPossible("The catalog has no other date for this event")
    elif scenario == DemoScenario.wrong_quantity:
        # the same money as the clean order spread over one more unit. a total that grew would
        # trip the price cap first, and then this would be testing the rule and not Claude.
        # one more and not a dozen, so the unit price still looks like a real price on camera
        clean_unit_price = (target_total / quantity).quantize(CENT, rounding=ROUND_DOWN)
        target_total = clean_unit_price * quantity
        quantity += 1

    unit_price = (target_total / quantity).quantize(CENT, rounding=rounding)
    try:
        # name, color and size stay separate fields, like an agent's order. item_display_name in
        # orders.py and describeItem in the frontend join them into "Trail runners, blue, size 8",
        # so putting the color in the name as well would print it twice
        item = OrderItemIn(
            name=item_name,
            color=color,
            size=size,
            model=model,
            event_date=event_date,
            quantity=quantity,
            unit_price=unit_price,
        )
    except ValidationError as error:
        # a cap of one cent, or a quantity or price past what POST /orders accepts
        raise ScenarioNotPossible("This mandate's values can't make a valid order") from error

    return DemoOrder(items=[item], shipping=DEMO_SHIPPING, merchant=merchant)


def require_catalog(catalog_rows):
    # the mandate's category isn't one the store sells (or the parser didn't give one), so there's
    # no real sibling variant to pick
    if not catalog_rows:
        raise ScenarioNotPossible("This mandate isn't for anything in the store's catalog")


def product_name(item):
    # "trail runners" from the parser reads as "Trail runners" on a product line. None if blank
    if not item or not item.strip():
        return None
    name = item.strip()
    return name[0].upper() + name[1:]


def clean_total(mandate):
    if mandate.max_price is None:
        return PRICE_WITHOUT_CAP
    return Decimal(str(mandate.max_price)) * CLEAN_SHARE_OF_CAP


def different_color(color):
    wanted = color.casefold()
    for candidate in WRONG_COLORS:
        if candidate not in wanted:
            return candidate
    # a mandate would have to name all of them for this to run
    return "purple"


def different_store(merchant):
    if normalize_merchant(merchant) == normalize_merchant(OTHER_STORE_NAME):
        return STORE_NAME
    return OTHER_STORE_NAME


@router.post(
    "/orders",
    response_model=OrderDetail,
    status_code=201,
    dependencies=[Depends(require_merchant)],
)
def create_demo_order(body: DemoOrderCreate, session: DbSession, paypal: PayPal, claude: Claude):
    # checked first so a missing setting can't leave half an order behind
    vault_id = get_settings().demo_vault_id
    if not vault_id:
        raise HTTPException(status_code=503, detail="DEMO_VAULT_ID isn't set on the API")

    mandate = session.get(Mandate, body.mandate_id)
    if mandate is None:
        raise HTTPException(
            status_code=404, detail="Mandate " + str(body.mandate_id) + " not found"
        )

    catalog_rows = catalog.load_category(session, mandate.rules.get("category"))
    try:
        demo_order = build_demo_order(mandate, body.scenario, catalog_rows)
    except ScenarioNotPossible as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    order, _ = place_order(
        session,
        mandate,
        demo_order.items,
        demo_order.shipping,
        vault_id,
        paypal,
        claude,
        merchant=demo_order.merchant,
        agent_name=DEMO_AGENT_NAME,
    )

    # read it back, so this shows what got saved and not what the session remembers
    order_id = order.id
    session.expire_all()
    return load_order(session, order_id)


@router.delete("/seeded", dependencies=[Depends(require_merchant)])
def delete_seeded(session: DbSession):
    # refuses while any seeded hold is still open, so deleting the row can't leave an authorization
    # at PayPal that nothing knows about. the 409 lists them so the script can void them first.
    # captures stay at PayPal, this only forgets them here
    mandate_query = select(Mandate.id).where(Mandate.buyer_label.like(SEED_LABEL_PREFIX + "%"))
    mandate_ids = list(session.scalars(mandate_query).all())

    order_query = select(Order).where(Order.mandate_id.in_(mandate_ids)).with_for_update()
    orders = session.scalars(order_query).all()

    open_order_ids = []
    order_ids = []
    for order in orders:
        order_ids.append(order.id)
        if order.status == OrderStatus.authorized and not hold_has_expired(order):
            open_order_ids.append(order.id)

    if open_order_ids:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Void the open holds on these orders first",
                "open_order_ids": open_order_ids,
            },
        )

    # a dispute points at its order, so it has to go first or Postgres refuses the order delete.
    # same deal as captures: the dispute stays at PayPal, this only forgets it here
    deleted_disputes = session.execute(
        delete(Dispute).where(Dispute.order_id.in_(order_ids))
    ).rowcount
    session.execute(delete(Approval).where(Approval.order_id.in_(order_ids)))
    session.execute(delete(Decision).where(Decision.order_id.in_(order_ids)))
    session.execute(delete(Order).where(Order.id.in_(order_ids)))
    session.execute(delete(Mandate).where(Mandate.id.in_(mandate_ids)))
    session.commit()
    return {
        "deleted_orders": len(order_ids),
        "deleted_mandates": len(mandate_ids),
        "deleted_disputes": deleted_disputes,
    }
