# seed_demo.py against the real app, with Claude and PayPal faked. The fakes answer by reading what
# they're sent, so this checks the script's flow and bookkeeping. Whether real Claude flags these
# orders is only something a live run can tell.

import re
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from keyed_client import AGENT_KEY, MERCHANT_HEADERS, MERCHANT_KEY
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import demo, mandates, orders, review
from backend.app.db import Base, get_session
from backend.app.models import Approval, Decision, Dispute, Mandate, Order, OrderStatus
from backend.scripts import seed_demo

VAULT_ID = "test-vault"

# the script sets a real timeout on every call, and TestClient grumbles about being given one
pytestmark = pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")


class FakeClaude:
    # both Claude calls come through here: parsing mandates, and checking orders

    def __init__(self):
        self.always_match = False
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def with_options(self, **options):
        return self

    def _create(self, **kwargs):
        name = kwargs["tools"][0]["name"]
        content = kwargs["messages"][0]["content"]
        if name == "record_mandate":
            fields = self.parse_mandate(content)
        else:
            fields = self.check_order(content)
        block = SimpleNamespace(type="tool_use", name=name, input=fields)
        return SimpleNamespace(stop_reason="tool_use", content=[block])

    def parse_mandate(self, content):
        request = content.split("<request>")[1]
        price = re.search(r"\$(\d+)", request)
        expired = "January 15, 2026" in request
        merchant = "Ridgeline Outdoors" if "Ridgeline" in request else "Fernwood Supply Co"
        return {
            "item": "something",
            "category": None,
            "color": None,
            "size": None,
            "quantity": None,
            "max_price": int(price.group(1)),
            "currency": "USD",
            "max_price_inclusive": True,
            "merchant": merchant,
            "expires_at": "2026-01-15T23:59:59+00:00" if expired else None,
            "notes": None,
        }

    def check_order(self, content):
        order_text = content.split("<order>")[1]
        fields = []
        if not self.always_match:
            if '"red"' in order_text:
                fields = ["color"]
            elif '"quantity": 4' in order_text:
                fields = ["quantity"]
            elif "Ignore previous" in order_text:
                fields = ["instructions_in_order"]
        if fields:
            return {"verdict": "mismatch", "risk_score": 90, "reason": "Doesn't match.",
                    "mismatched_fields": fields}
        return {"verdict": "match", "risk_score": 5, "reason": "Matches.",
                "mismatched_fields": []}


class FakePayPal:
    def __init__(self):
        self.created = 0
        self.captured = []
        self.voided = []
        self.void_fails = False

    def create_order(self, body, request_id=None):
        self.created += 1
        return {"id": "PP" + uuid.uuid4().hex[:10], "status": "COMPLETED", "purchase_units": [{
            "payments": {"authorizations": [{
                "id": "AUTH" + uuid.uuid4().hex[:10],
                "status": "CREATED",
                "expiration_time": "2099-01-01T00:00:00Z",
            }]}
        }]}

    def capture_authorization(self, authorization_id, request_id=None, budget_seconds=None):
        self.captured.append(authorization_id)
        return {"id": "CAP" + authorization_id, "status": "COMPLETED"}

    def void_authorization(self, authorization_id, request_id=None):
        if self.void_fails:
            raise seed_demo.requests.ConnectionError("PayPal is down")
        self.voided.append(authorization_id)
        return {"id": authorization_id, "status": "VOIDED"}


@pytest.fixture
def sessions():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    # SQLite ignores foreign keys unless asked, Postgres never does. without this a delete that
    # Render refuses (an order with a dispute on it) passes here
    @event.listens_for(engine, "connect")
    def enforce_foreign_keys(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def claude():
    return FakeClaude()


@pytest.fixture
def paypal():
    return FakePayPal()


@pytest.fixture
def http(sessions, claude, paypal):
    app = FastAPI()
    app.include_router(mandates.router)
    app.include_router(orders.router)
    app.include_router(orders.agent_router)
    app.include_router(review.router)
    app.include_router(demo.router)

    def test_session():
        session = sessions()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = test_session
    app.dependency_overrides[mandates.get_anthropic_client] = lambda: claude
    app.dependency_overrides[orders.get_anthropic_client] = lambda: claude
    app.dependency_overrides[orders.get_paypal_client] = lambda: paypal
    return TestClient(app)


@pytest.fixture
def api(http):
    return seed_demo.SeedApi(http, "", AGENT_KEY, MERCHANT_KEY)


def result_for(results, name):
    for result in results:
        if result["scenario"]["name"] == name:
            return result
    raise AssertionError("no scenario " + name)


# ---- the scenarios themselves ----


def test_ten_clean_and_seven_sneaky():
    expected_states = []
    for scenario in seed_demo.SCENARIOS:
        expected_states.append(scenario["expected"])

    assert expected_states.count("auto_approved") == 10
    assert expected_states.count("needs_review") == 7


def scenario_named(name):
    for scenario in seed_demo.SCENARIOS:
        if scenario["name"] == name:
            return scenario
    raise AssertionError("no scenario " + name)


def test_one_clean_mandate_per_catalog_category():
    # these are the mandates /place-order breaks on camera, one per catalog category
    names = ["clean: white sneakers", "clean: oat milk", "clean: earbuds",
             "clean: market tickets", "clean: pantry box"]
    for name in names:
        assert scenario_named(name)["expected"] == "auto_approved"

    earbuds = scenario_named("clean: earbuds")["items"][0]
    assert earbuds["model"] == "FP-200"
    tickets = scenario_named("clean: market tickets")["items"][0]
    assert tickets["event_date"] == "2026-12-05"
    assert tickets["quantity"] == 2


def test_scenarios_are_all_at_the_fictional_stores():
    for scenario in seed_demo.SCENARIOS:
        assert "Fernwood Supply Co" in scenario["request"] or (
            "Ridgeline Outdoors" in scenario["request"]
        )


def test_each_sneaky_scenario_is_caught_for_its_own_reason(api, capsys):
    results = seed_demo.run_seed(api, VAULT_ID)

    assert seed_demo.print_results(results) == 0
    over_cap = result_for(results, "sneaky: over the cap")["order"]["intent_check"]
    assert "$115.00 item + $10.00 shipping" in over_cap["reason"]
    assert over_cap["checked_by"] == "rules"
    store = result_for(results, "sneaky: wrong store")["order"]["intent_check"]
    assert store["mismatched_fields"] == ["merchant"]
    expired = result_for(results, "sneaky: expired mandate")["order"]["intent_check"]
    assert expired["mismatched_fields"] == ["expiry"]
    injection = result_for(results, "sneaky: injection in name")["order"]["intent_check"]
    assert injection["reason"] == "Order text tried to instruct the checker"


def test_twelve_at_full_price_is_stopped_by_the_price_cap(api, claude, capsys):
    # a year of the plan at the catalog's $29, against "one month, max $35". the price rule has to
    # catch it on its own, before Claude ever gets asked about the quantity
    scenario = scenario_named("sneaky: 12 at full price")
    assert scenario["items"][0]["quantity"] == 12
    assert scenario["items"][0]["unit_price"] == "29.00"

    results = seed_demo.run_seed(api, VAULT_ID, [scenario])

    assert seed_demo.print_results(results) == 0
    order = results[0]["order"]
    assert order["total"] == "348.00"
    assert order["status"] == "authorized"
    check = order["intent_check"]
    assert check["checked_by"] == "rules"
    assert check["mismatched_fields"] == ["price"]
    assert check["reason"] == "Order is $348.00, mandate cap is $35.00"


def test_catalog_items_keep_their_model_and_date(api, capsys):
    scenarios = [scenario_named("clean: earbuds"), scenario_named("clean: market tickets")]

    results = seed_demo.run_seed(api, VAULT_ID, scenarios)

    assert seed_demo.print_results(results) == 0
    assert results[0]["order"]["items"][0]["model"] == "FP-200"
    assert results[1]["order"]["items"][0]["event_date"] == "2026-12-05"


# ---- seeding ----


def test_clean_orders_capture_and_sneaky_ones_wait(api, paypal, capsys):
    results = seed_demo.run_seed(api, VAULT_ID)

    assert len(paypal.captured) == 10
    assert paypal.created == 17
    for result in results:
        assert result["order"]["review_state"] == result["scenario"]["expected"]
        assert result["created"] is True


def test_review_links_only_for_orders_waiting_and_they_work(api, http, capsys):
    results = seed_demo.run_seed(api, VAULT_ID)

    for result in results:
        if result["scenario"]["expected"] == "needs_review":
            assert http.get(result["link"]).status_code == 200
        else:
            assert result["link"] is None


def test_table_shows_every_scenario_and_the_links(api, capsys):
    results = seed_demo.run_seed(api, VAULT_ID)
    capsys.readouterr()

    seed_demo.print_results(results)

    output = capsys.readouterr().out
    for scenario in seed_demo.SCENARIOS:
        assert scenario["name"] in output
    for heading in ["SCENARIO", "EXPECTED", "ACTUAL", "RISK", "REASON"]:
        assert heading in output
    assert output.count("/review/") == 7
    assert "17 of 17 as expected" in output


def test_running_it_again_makes_no_duplicates(api, paypal, sessions, capsys):
    seed_demo.run_seed(api, VAULT_ID)

    results = seed_demo.run_seed(api, VAULT_ID)

    assert seed_demo.print_results(results) == 0
    assert paypal.created == 17
    assert len(paypal.captured) == 10
    with sessions() as session:
        assert session.query(Order).count() == 17
        assert session.query(Mandate).count() == 17
    for result in results:
        assert result["created"] is False


def test_running_it_again_makes_new_links_that_replace_the_old(api, http, capsys):
    first = seed_demo.run_seed(api, VAULT_ID)
    old_link = result_for(first, "sneaky: wrong color")["link"]

    second = seed_demo.run_seed(api, VAULT_ID)

    assert http.get(old_link).status_code == 404
    assert http.get(result_for(second, "sneaky: wrong color")["link"]).status_code == 200


def test_a_partial_run_only_fills_in_whats_missing(api, paypal, capsys):
    seed_demo.run_seed(api, VAULT_ID, seed_demo.SCENARIOS[:3])
    assert paypal.created == 3

    results = seed_demo.run_seed(api, VAULT_ID)

    assert paypal.created == 17
    assert seed_demo.print_results(results) == 0


def test_rerun_after_a_human_decided_still_passes(api, http, paypal, capsys):
    first = seed_demo.run_seed(api, VAULT_ID)
    approve_link = result_for(first, "sneaky: wrong color")["link"]
    reject_link = result_for(first, "sneaky: odd quantity")["link"]
    assert http.post(approve_link + "/approve").status_code == 200
    assert http.post(reject_link + "/reject").status_code == 200

    results = seed_demo.run_seed(api, VAULT_ID)

    assert seed_demo.print_results(results) == 0
    assert result_for(results, "sneaky: wrong color")["link"] is None
    assert result_for(results, "sneaky: wrong color")["order"]["review_state"] == "approved"
    assert result_for(results, "sneaky: odd quantity")["order"]["review_state"] == "rejected"


def test_wrong_outcome_exits_non_zero(api, claude, capsys):
    # the wrong color gets past the check and auto-captures, which is the bug this exists to catch
    claude.always_match = True

    results = seed_demo.run_seed(api, VAULT_ID)

    assert seed_demo.print_results(results) == 1
    output = capsys.readouterr().out
    assert "WRONG: sneaky: wrong color: expected needs_review, got auto_approved" in output


def test_right_state_for_the_wrong_reason_exits_non_zero(api, capsys):
    # still needs_review, but not because of what the scenario is meant to show
    broken = dict(scenario_named("sneaky: over the cap"))
    broken["reason_contains"] = "something it won't say"

    results = seed_demo.run_seed(api, VAULT_ID, [broken])

    assert seed_demo.print_results(results) == 1


def test_api_error_on_one_scenario_is_reported_and_exits_non_zero(http, capsys):
    # wrong agent key, so nothing can be created. it has to say so and not stop silently
    api = seed_demo.SeedApi(http, "", "wrong-key", MERCHANT_KEY)

    results = seed_demo.run_seed(api, VAULT_ID, seed_demo.SCENARIOS[:2])

    assert seed_demo.print_results(results) == 1
    assert "returned 401" in capsys.readouterr().out


# ---- resetting ----


def test_reset_voids_open_holds_then_deletes_everything(api, paypal, sessions, capsys):
    seed_demo.run_seed(api, VAULT_ID)

    seed_demo.run_reset(api)

    # the 7 that were waiting. the 10 clean ones were captured, nothing to void
    assert len(paypal.voided) == 7
    with sessions() as session:
        assert session.query(Order).count() == 0
        assert session.query(Mandate).count() == 0
        assert session.query(Decision).count() == 0
        assert session.query(Approval).count() == 0


def test_reset_also_clears_disputes_on_seeded_orders(api, sessions, capsys):
    # a PayPal dispute synced onto a seeded order used to make Postgres refuse the order delete
    seed_demo.run_seed(api, VAULT_ID, seed_demo.SCENARIOS[:1])
    with sessions() as session:
        order = session.query(Order).one()
        session.add(Dispute(paypal_dispute_id="PP-R-TEST-1", order_id=order.id))
        session.commit()

    seed_demo.run_reset(api)

    with sessions() as session:
        assert session.query(Dispute).count() == 0
        assert session.query(Order).count() == 0
    assert "Also forgot 1 disputes" in capsys.readouterr().out


def test_reset_with_nothing_seeded_is_fine(api, paypal, capsys):
    seed_demo.run_reset(api)

    assert paypal.voided == []
    assert "Deleted 0 orders" in capsys.readouterr().out


def test_can_seed_again_after_a_reset(api, paypal, capsys):
    seed_demo.run_seed(api, VAULT_ID)
    seed_demo.run_reset(api)

    results = seed_demo.run_seed(api, VAULT_ID)

    assert paypal.created == 34
    assert seed_demo.print_results(results) == 0


def test_failed_void_keeps_the_rows_so_the_hold_isnt_forgotten(api, paypal, sessions, capsys):
    seed_demo.run_seed(api, VAULT_ID)
    paypal.void_fails = True

    with pytest.raises(seed_demo.ApiError):
        seed_demo.run_reset(api)

    with sessions() as session:
        assert session.query(Order).count() == 17
        open_holds = session.query(Order).filter_by(status=OrderStatus.authorized).count()
        assert open_holds == 7


def test_reset_leaves_other_orders_alone(api, http, sessions, capsys):
    seed_demo.run_seed(api, VAULT_ID, seed_demo.SCENARIOS[:1])
    with sessions() as session:
        session.add(Mandate(id=500, buyer_label="Sam", raw_request="x", rules={}))
        session.add(Order(
            id=500, mandate_id=500, items=[], total=1, currency="USD",
            status=OrderStatus.authorized,
            authorization_expires_at=datetime.now(timezone.utc) + timedelta(days=1),
        ))
        session.commit()

    seed_demo.run_reset(api)

    with sessions() as session:
        assert session.query(Order).one().id == 500
        assert session.query(Mandate).one().id == 500


def test_delete_refuses_while_a_hold_is_open_and_says_which(api, http, capsys):
    scenarios = [scenario_named("sneaky: wrong color"), scenario_named("sneaky: over the cap")]
    results = seed_demo.run_seed(api, VAULT_ID, scenarios)

    response = http.delete("/demo/seeded", headers=MERCHANT_HEADERS)

    assert response.status_code == 409
    expected_ids = sorted(result["order"]["id"] for result in results)
    assert sorted(response.json()["detail"]["open_order_ids"]) == expected_ids


def test_expired_hold_doesnt_block_deleting(api, http, sessions, capsys):
    seed_demo.run_seed(api, VAULT_ID, [scenario_named("sneaky: wrong color")])
    with sessions() as session:
        order = session.query(Order).one()
        order.authorization_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.commit()

    response = http.delete("/demo/seeded", headers=MERCHANT_HEADERS)

    assert response.status_code == 200
    assert response.json()["deleted_orders"] == 1


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": AGENT_KEY}, {"X-API-Key": "wrong"}])
def test_delete_needs_the_merchant_key(http, headers):
    assert http.delete("/demo/seeded", headers=headers).status_code == 401
