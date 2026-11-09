# POST /mandates and GET /mandates/{id} with a fake Anthropic client and SQLite

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from keyed_client import AGENT_HEADERS, MERCHANT_HEADERS, KeyedClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import catalog, mandate_parser, mandates
from backend.app.db import Base, get_session
from backend.app.models import Mandate
from backend.app.prompts import mandate as mandate_prompt

FERNWOOD_REQUEST = (
    "Get me black trail runners, women's size 9, under $120, from Fernwood Supply Co, "
    "sometime this week."
)


class FakeAnthropic:
    # stands in for anthropic.Anthropic. hands back canned responses in order
    # and remembers every request so tests can check what we sent

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


def tool_call(fields):
    block = SimpleNamespace(type="tool_use", name="record_mandate", input=fields)
    return SimpleNamespace(stop_reason="tool_use", content=[block])


def text_only(text):
    block = SimpleNamespace(type="text", text=text)
    return SimpleNamespace(stop_reason="end_turn", content=[block])


ALL_NULL = {
    "item": None,
    "category": None,
    "color": None,
    "size": None,
    "quantity": None,
    "max_price": None,
    "currency": None,
    # strict mode always sends it, and the prompt says true when there's no price
    "max_price_inclusive": True,
    "merchant": None,
    "expires_at": None,
    "notes": None,
}


@pytest.fixture
def db_sessions():
    # one in-memory SQLite shared by every session in the test
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def make_app(db_sessions, fake_client):
    app = FastAPI()
    app.include_router(mandates.router)

    def test_session():
        session = db_sessions()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = test_session
    app.dependency_overrides[mandates.get_anthropic_client] = lambda: fake_client
    return KeyedClient(app)


def plain_client(db_sessions, fake_client):
    # no key added for it, these tests choose their own
    return TestClient(make_app(db_sessions, fake_client).app)


def test_creating_a_mandate_needs_the_agent_key(db_sessions):
    fake = FakeAnthropic([])
    client = plain_client(db_sessions, fake)
    body = {"buyer_label": "Sam", "request_text": "x"}

    assert client.post("/mandates", json=body).status_code == 401
    assert client.post("/mandates", json=body, headers=MERCHANT_HEADERS).status_code == 401
    # Claude costs money, so it can't get as far as that without a key
    assert fake.requests == []


def test_reading_a_mandate_needs_the_merchant_key(db_sessions):
    client = plain_client(db_sessions, FakeAnthropic([]))

    assert client.get("/mandates/1").status_code == 401
    assert client.get("/mandates/1", headers=AGENT_HEADERS).status_code == 401
    # right key, and now it gets as far as looking for one
    assert client.get("/mandates/1", headers=MERCHANT_HEADERS).status_code == 404


def test_tool_output_maps_to_db_row(db_sessions):
    fake = FakeAnthropic([
        tool_call({
            "item": "trail runners",
            "category": "footwear",
            "color": "black",
            "size": "women's 9",
            "quantity": None,
            "max_price": 120,
            # "under $120": the total has to be below 120
            "max_price_inclusive": False,
            "merchant": "Fernwood Supply Co",
            "expires_at": "2026-10-25T23:59:59+00:00",
            "notes": None,
        })
    ])
    client = make_app(db_sessions, fake)

    response = client.post(
        "/mandates",
        json={"buyer_label": "Sam", "request_text": FERNWOOD_REQUEST, "merchant": None},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["max_price"] == "120.00"
    assert body["max_price_inclusive"] is False
    assert body["rules"]["max_price_inclusive"] is False
    assert body["buyer_label"] == "Sam"
    assert body["raw_request"] == FERNWOOD_REQUEST
    assert body["merchant"] == "Fernwood Supply Co"
    assert body["rules"]["item"] == "trail runners"
    assert body["rules"]["size"] == "women's 9"
    # shopper didn't give a quantity, we default to 1
    assert body["rules"]["quantity"] == 1

    with db_sessions() as session:
        row = session.get(Mandate, body["id"])
        assert row.max_price == Decimal("120.00")
        assert row.max_price_inclusive is False
        assert row.expires_at.replace(tzinfo=timezone.utc) == datetime(
            2026, 10, 25, 23, 59, 59, tzinfo=timezone.utc
        )
        assert row.rules["color"] == "black"

    # and it reads back the same through GET
    fetched = client.get("/mandates/" + str(body["id"]))
    assert fetched.status_code == 200
    assert fetched.json()["rules"] == body["rules"]


def test_model_returns_nulls(db_sessions):
    fake = FakeAnthropic([tool_call(ALL_NULL)])
    client = make_app(db_sessions, fake)

    response = client.post(
        "/mandates",
        json={"buyer_label": "Sam", "request_text": "get me something nice"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["max_price"] is None
    assert body["merchant"] is None
    assert body["expires_at"] is None
    for name in ("item", "category", "color", "size", "max_price", "merchant", "notes"):
        assert body["rules"][name] is None


def test_no_price_given_keeps_max_price_null(db_sessions):
    # the shopper names everything except a price. nothing should invent one,
    # and quantity defaulting to 1 must not leak into max_price
    request_text = "Get me black trail runners, women's size 9, from Fernwood Supply Co."
    fake = FakeAnthropic([
        tool_call({
            "item": "trail runners",
            "category": "footwear",
            "color": "black",
            "size": "women's 9",
            "quantity": None,
            "max_price": None,
            "merchant": "Fernwood Supply Co",
            "expires_at": None,
            "notes": None,
        })
    ])
    client = make_app(db_sessions, fake)

    response = client.post(
        "/mandates", json={"buyer_label": "Sam", "request_text": request_text}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["max_price"] is None
    assert body["rules"]["max_price"] is None
    # the other fields still came through
    assert body["rules"]["item"] == "trail runners"
    assert body["rules"]["quantity"] == 1

    with db_sessions() as session:
        assert session.get(Mandate, body["id"]).max_price is None


def test_up_to_price_is_inclusive(db_sessions):
    fields = dict(ALL_NULL)
    fields["max_price"] = 120
    fields["max_price_inclusive"] = True
    client = make_app(db_sessions, FakeAnthropic([tool_call(fields)]))

    response = client.post(
        "/mandates", json={"buyer_label": "Sam", "request_text": "trail runners, up to $120"}
    )

    assert response.json()["max_price_inclusive"] is True
    with db_sessions() as session:
        assert session.get(Mandate, response.json()["id"]).max_price_inclusive is True


def test_euro_cap_keeps_the_amount_and_the_currency(db_sessions):
    # "under 100 euros" stores 100 + EUR, no conversion. this used to come through as a null
    # max_price with the amount buried in notes, which let the order slip past the rules
    fields = dict(ALL_NULL)
    fields["item"] = "rain jacket"
    fields["max_price"] = 100
    fields["currency"] = "EUR"
    fields["max_price_inclusive"] = False
    client = make_app(db_sessions, FakeAnthropic([tool_call(fields)]))

    response = client.post(
        "/mandates", json={"buyer_label": "Sam", "request_text": "A rain jacket under 100 euros"}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["max_price"] == "100.00"
    assert body["max_price_inclusive"] is False
    assert body["rules"]["max_price"] == 100
    assert body["rules"]["currency"] == "EUR"

    with db_sessions() as session:
        row = session.get(Mandate, body["id"])
        assert row.max_price == Decimal("100.00")
        assert row.rules["currency"] == "EUR"


def test_dollar_cap_records_usd(db_sessions):
    fields = dict(ALL_NULL)
    fields["max_price"] = 120
    fields["currency"] = "USD"
    client = make_app(db_sessions, FakeAnthropic([tool_call(fields)]))

    response = client.post(
        "/mandates", json={"buyer_label": "Sam", "request_text": "trail runners, up to $120"}
    )

    assert response.json()["max_price"] == "120.00"
    assert response.json()["rules"]["currency"] == "USD"


def test_currency_stays_null_when_not_named(db_sessions):
    fields = dict(ALL_NULL)
    fields["max_price"] = 100
    client = make_app(db_sessions, FakeAnthropic([tool_call(fields)]))

    response = client.post(
        "/mandates", json={"buyer_label": "Sam", "request_text": "a budget of 100"}
    )

    assert response.json()["max_price"] == "100.00"
    assert response.json()["rules"]["currency"] is None


def test_currency_is_tidied_up():
    lowercase = dict(ALL_NULL)
    lowercase["currency"] = " eur "
    blank = dict(ALL_NULL)
    blank["currency"] = "  "
    missing = dict(ALL_NULL)
    del missing["currency"]

    assert mandate_parser.clean_fields(lowercase)["currency"] == "EUR"
    assert mandate_parser.clean_fields(blank)["currency"] is None
    assert mandate_parser.clean_fields(missing)["currency"] is None


def test_currency_is_in_the_tool_schema():
    # strict mode needs every property listed in required, and it has to accept null
    schema = mandate_prompt.RECORD_MANDATE_TOOL["input_schema"]

    assert schema["properties"]["currency"]["type"] == ["string", "null"]
    assert "currency" in schema["required"]
    assert "max_price" in schema["required"]
    # the old instruction threw the amount away for non-USD caps
    assert "set max_price to null" not in mandate_prompt.SYSTEM_PROMPT


def test_inclusive_defaults_to_true_when_unclear():
    # strict mode should always send a boolean, but if it's ever missing or null,
    # treat it as "up to" rather than tightening the limit
    missing = dict(ALL_NULL)
    del missing["max_price_inclusive"]
    nulled = dict(ALL_NULL)
    nulled["max_price_inclusive"] = None

    assert mandate_parser.clean_fields(missing)["max_price_inclusive"] is True
    assert mandate_parser.clean_fields(nulled)["max_price_inclusive"] is True


def test_main_app_serves_mandate_routes():
    from backend.app.main import app

    # the OpenAPI doc lists every served path, however the routers are nested
    paths = app.openapi()["paths"]
    assert "post" in paths["/mandates"]
    assert "get" in paths["/mandates/{mandate_id}"]


def test_request_merchant_wins_over_parsed_one(db_sessions):
    fields = dict(ALL_NULL)
    fields["merchant"] = "Some Other Shop"
    client = make_app(db_sessions, FakeAnthropic([tool_call(fields)]))

    response = client.post(
        "/mandates",
        json={"buyer_label": "Sam", "request_text": "x", "merchant": "Fernwood Supply Co"},
    )

    assert response.json()["merchant"] == "Fernwood Supply Co"
    # what the shopper said is still kept in rules
    assert response.json()["rules"]["merchant"] == "Some Other Shop"


def test_request_shape_sent_to_claude(db_sessions):
    fake = FakeAnthropic([tool_call(ALL_NULL)])
    client = make_app(db_sessions, fake)

    client.post("/mandates", json={"buyer_label": "Sam", "request_text": FERNWOOD_REQUEST})

    sent = fake.requests[0]
    assert sent["model"] == "claude-sonnet-5-5"
    # forced tool_choice is a 400 on Sonnet 5.5, so it has to be auto
    assert sent["tool_choice"] == {"type": "auto"}
    assert sent["tools"][0]["name"] == "record_mandate"
    assert sent["tools"][0]["strict"] is True
    assert sent["fallbacks"] == "default"
    # the current date has to reach the model so "this week" can be resolved
    assert "Current date and time:" in sent["messages"][0]["content"]
    assert FERNWOOD_REQUEST in sent["messages"][0]["content"]


def test_retries_once_when_no_tool_call(db_sessions):
    fields = dict(ALL_NULL)
    fields["item"] = "trail runners"
    fake = FakeAnthropic([text_only("Sure!"), tool_call(fields)])
    client = make_app(db_sessions, fake)

    response = client.post("/mandates", json={"buyer_label": "Sam", "request_text": "x"})

    assert response.status_code == 201
    assert len(fake.requests) == 2
    assert response.json()["rules"]["item"] == "trail runners"


def test_gives_up_with_502(db_sessions):
    fake = FakeAnthropic([text_only("no"), text_only("still no")])
    client = make_app(db_sessions, fake)

    response = client.post("/mandates", json={"buyer_label": "Sam", "request_text": "x"})

    assert response.status_code == 502


def test_refusal_is_a_502(db_sessions):
    refusal = SimpleNamespace(stop_reason="refusal", content=[])
    client = make_app(db_sessions, FakeAnthropic([refusal]))

    response = client.post("/mandates", json={"buyer_label": "Sam", "request_text": "x"})

    assert response.status_code == 502


def test_get_missing_mandate_is_404(db_sessions):
    client = make_app(db_sessions, FakeAnthropic([]))

    assert client.get("/mandates/999").status_code == 404


def test_bad_date_from_model_is_not_stored():
    fields = dict(ALL_NULL)
    fields["expires_at"] = "sometime soon"
    assert mandate_parser.expires_at_datetime(fields) is None


def test_model_and_event_date_are_in_the_tool_schema():
    # strict mode needs every property in required, nullable so "not given" can be said
    schema = mandate_prompt.RECORD_MANDATE_TOOL["input_schema"]

    for name in ["model", "event_date"]:
        assert schema["properties"][name]["type"] == ["string", "null"]
        assert name in schema["required"]


def test_the_catalog_categories_are_in_the_prompt():
    # the demo finds a mandate's catalog rows by these exact words
    for category in catalog.CATEGORIES:
        assert '"' + category + '"' in mandate_prompt.SYSTEM_PROMPT


def test_model_and_event_date_reach_the_rules(db_sessions):
    fields = dict(ALL_NULL)
    fields["item"] = "Fall Makers Market tickets"
    fields["category"] = "event tickets"
    fields["model"] = None
    fields["event_date"] = "2026-12-05"
    fields["quantity"] = 2
    client = make_app(db_sessions, FakeAnthropic([tool_call(fields)]))

    response = client.post(
        "/mandates", json={"buyer_label": "Sam", "request_text": "Two tickets for December 5"}
    )

    assert response.status_code == 201
    assert response.json()["rules"]["event_date"] == "2026-12-05"
    assert response.json()["rules"]["model"] is None


def test_blank_model_is_not_given():
    fields = dict(ALL_NULL)
    fields["model"] = "   "

    assert mandate_parser.clean_fields(fields)["model"] is None
