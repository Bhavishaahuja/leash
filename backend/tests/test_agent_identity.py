# Which shopping agent placed an order. The name comes from the agent's API key (AGENT_API_KEYS)
# and never from the request body, so one agent can't file its orders under another's name.

import uuid
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from fastapi.testclient import TestClient
from keyed_client import AGENT_KEY, MERCHANT_HEADERS, MERCHANT_KEY
from pydantic import ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app import demo, mandates, orders
from backend.app.config import (
    DEFAULT_AGENT_NAME,
    Settings,
    agent_keys,
    get_settings,
    parse_agent_api_keys,
)
from backend.app.db import Base, get_session
from backend.app.models import Mandate, Order

WREN_KEY = "wren-test-key"
THISTLE_KEY = "thistle-test-key"
AGENTS_SETTING = WREN_KEY + ":Wren," + THISTLE_KEY + ":Thistle"
VAULT_ID = "test-agent-vault"


# ---- reading AGENT_API_KEYS ----


def test_comma_list():
    assert parse_agent_api_keys(" k1 : Wren ,k2:Thistle Errands, ") == {
        "k1": "Wren", "k2": "Thistle Errands",
    }


def test_json_object():
    setting = '{"k1": "Wren", "k2": "Thistle, the errand one"}'

    assert parse_agent_api_keys(setting) == {"k1": "Wren", "k2": "Thistle, the errand one"}


def test_empty_means_no_named_agents():
    assert parse_agent_api_keys("") == {}
    assert parse_agent_api_keys("   ") == {}


@pytest.mark.parametrize(
    ("setting", "problem"),
    [
        ("k1-no-name", "key:Name"),
        ("k1:,k2:Thistle", "needs a key and a name"),
        (":Wren", "needs a key and a name"),
        ("k1:Wren,k1:Thistle", "twice"),
        ('{"k1": "Wren"', "valid JSON"),
        ('["k1", "Wren"]', "object"),
        ('{"k1": 7}', "text"),
        ("k1:" + "W" * 101, "too long"),
    ],
)
def test_a_setting_it_cant_read_for_sure_is_refused(setting, problem):
    with pytest.raises(ValueError, match=problem):
        parse_agent_api_keys(setting)


def test_a_bad_setting_stops_settings_loading():
    # a typo has to stop the API starting, not quietly lock an agent out
    with pytest.raises(ValidationError):
        Settings(agent_api_keys="k1-no-name", _env_file=None)


def test_an_agent_key_cant_be_the_merchant_key():
    with pytest.raises(ValidationError, match="MERCHANT_API_KEY"):
        Settings(agent_api_keys="same:Wren", merchant_api_key="same", _env_file=None)


def test_the_old_single_key_still_works_under_the_default_name():
    settings = Settings(agent_api_key="old-key", agent_api_keys=AGENTS_SETTING, _env_file=None)

    assert agent_keys(settings) == {
        WREN_KEY: "Wren", THISTLE_KEY: "Thistle", "old-key": DEFAULT_AGENT_NAME,
    }


def test_agent_api_keys_can_rename_the_old_key():
    settings = Settings(agent_api_key="old-key", agent_api_keys="old-key:Wren", _env_file=None)

    assert agent_keys(settings) == {"old-key": "Wren"}


# ---- the routes ----


class AlwaysMatchClaude:
    def __init__(self):
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def with_options(self, **options):
        return self

    def _create(self, **kwargs):
        fields = {"verdict": "match", "risk_score": 5, "reason": "Matches.",
                  "mismatched_fields": []}
        block = SimpleNamespace(type="tool_use", name=kwargs["tools"][0]["name"], input=fields)
        return SimpleNamespace(stop_reason="tool_use", content=[block])


class FakePayPal:
    def __init__(self):
        self.bodies = []

    def create_order(self, body, request_id=None):
        self.bodies.append(body)
        return {"id": "PP" + uuid.uuid4().hex[:10], "status": "COMPLETED", "purchase_units": [{
            "payments": {"authorizations": [{
                "id": "AUTH" + uuid.uuid4().hex[:10],
                "status": "CREATED",
                "expiration_time": "2099-01-01T00:00:00Z",
            }]}
        }]}

    def capture_authorization(self, authorization_id, request_id=None, budget_seconds=None):
        return {"id": "CAP" + authorization_id, "status": "COMPLETED"}


@pytest.fixture
def sessions():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def paypal():
    return FakePayPal()


@pytest.fixture
def client(sessions, paypal, monkeypatch):
    monkeypatch.setenv("AGENT_API_KEYS", AGENTS_SETTING)
    monkeypatch.setenv("DEMO_VAULT_ID", VAULT_ID)
    get_settings.cache_clear()

    app = FastAPI()
    app.include_router(mandates.router)
    app.include_router(orders.router)
    app.include_router(demo.router)

    def test_session():
        session = sessions()
        try:
            yield session
        finally:
            session.close()

    claude = AlwaysMatchClaude()
    app.dependency_overrides[get_session] = test_session
    app.dependency_overrides[orders.get_anthropic_client] = lambda: claude
    app.dependency_overrides[orders.get_paypal_client] = lambda: paypal
    return TestClient(app)


@pytest.fixture
def mandate_id(sessions):
    with sessions() as session:
        mandate = Mandate(
            buyer_label="Sam",
            raw_request="One stainless steel camping mug, up to $40, from Fernwood Supply Co.",
            rules={"item": "stainless steel camping mug", "quantity": 1, "currency": "USD"},
            max_price=40,
            merchant="Fernwood Supply Co",
        )
        session.add(mandate)
        session.commit()
        return mandate.id


def place(client, mandate_id, key, **extra_body):
    body = {
        "mandate_id": mandate_id,
        "vault_id": VAULT_ID,
        "items": [{"name": "Stainless steel camping mug", "quantity": 1, "unit_price": "18.00"}],
    }
    body.update(extra_body)
    return client.post("/orders", json=body, headers={"X-API-Key": key})


def saved_agent_names(sessions):
    names = []
    with sessions() as session:
        for order in session.query(Order).order_by(Order.id):
            names.append(order.agent_name)
    return names


def test_the_name_comes_from_the_key(client, sessions, mandate_id):
    assert place(client, mandate_id, WREN_KEY).status_code == 201
    assert place(client, mandate_id, THISTLE_KEY).status_code == 201

    assert saved_agent_names(sessions) == ["Wren", "Thistle"]


def test_the_name_comes_from_the_key_even_when_the_body_claims_another(
    client, sessions, mandate_id
):
    # Wren's key, every way a body could try to say it's Thistle
    response = place(
        client, mandate_id, WREN_KEY,
        agent_name="Thistle", agent="Thistle", agentName="Thistle", name="Thistle",
    )

    assert response.status_code == 201
    assert saved_agent_names(sessions) == ["Wren"]
    detail = client.get("/orders/" + str(response.json()["id"]), headers=MERCHANT_HEADERS).json()
    assert detail["agent_name"] == "Wren"


def test_a_body_naming_a_real_agents_key_changes_nothing(client, sessions, mandate_id):
    # the key in the body isn't the key that counts, the header is
    response = place(client, mandate_id, WREN_KEY, api_key=THISTLE_KEY)

    assert response.status_code == 201
    assert saved_agent_names(sessions) == ["Wren"]


@pytest.mark.parametrize("key", ["", "not-a-real-key", WREN_KEY.upper(), WREN_KEY + " "])
def test_an_unknown_key_is_rejected_and_places_nothing(client, sessions, paypal, mandate_id, key):
    response = place(client, mandate_id, key)

    assert response.status_code == 401
    assert paypal.bodies == []
    assert saved_agent_names(sessions) == []


def test_no_key_at_all_is_rejected(client, sessions, paypal, mandate_id):
    body = {"mandate_id": mandate_id, "vault_id": VAULT_ID, "agent_name": "Wren",
            "items": [{"name": "Mug", "quantity": 1, "unit_price": "18.00"}]}

    assert client.post("/orders", json=body).status_code == 401
    assert paypal.bodies == []


def test_the_merchant_key_cant_place_an_order_as_any_agent(client, paypal, mandate_id):
    assert place(client, mandate_id, MERCHANT_KEY).status_code == 401
    assert paypal.bodies == []


def test_the_old_single_key_gets_the_default_name(client, sessions, mandate_id):
    # conftest sets AGENT_API_KEY. it keeps working so a deploy can't lock the live agent out
    assert place(client, mandate_id, AGENT_KEY).status_code == 201

    assert saved_agent_names(sessions) == [DEFAULT_AGENT_NAME]


def test_with_no_agents_configured_every_key_is_rejected(client, paypal, mandate_id, monkeypatch):
    monkeypatch.setenv("AGENT_API_KEYS", "")
    monkeypatch.setenv("AGENT_API_KEY", "")
    get_settings.cache_clear()

    for key in [WREN_KEY, AGENT_KEY, ""]:
        assert place(client, mandate_id, key).status_code == 401
    assert paypal.bodies == []


def test_every_agent_can_create_mandates(client):
    # the name only matters on orders, but every configured agent has to get in
    for key in [WREN_KEY, THISTLE_KEY]:
        response = client.post(
            "/mandates", json={"buyer_label": "Sam", "request_text": "x"},
            headers={"X-API-Key": key},
        )
        # past auth: no Claude is set up for mandates here, so it fails later, not with a 401
        assert response.status_code != 401


def test_the_agent_never_sees_its_own_name_back(client, mandate_id):
    # the agent gets four fields, same as before
    placed = place(client, mandate_id, WREN_KEY).json()

    assert set(placed) == {"id", "status", "review_state", "total"}


def test_the_merchant_sees_the_name_in_the_list_and_the_detail(client, mandate_id):
    order_id = place(client, mandate_id, THISTLE_KEY).json()["id"]

    listed = client.get("/orders", headers=MERCHANT_HEADERS).json()[0]
    detail = client.get("/orders/" + str(order_id), headers=MERCHANT_HEADERS).json()

    assert listed["agent_name"] == "Thistle"
    assert detail["agent_name"] == "Thistle"


def test_a_demo_page_order_is_credited_to_the_demo_page(client, sessions, mandate_id):
    response = client.post(
        "/demo/orders", json={"mandate_id": mandate_id, "scenario": "clean"},
        headers=MERCHANT_HEADERS,
    )

    assert response.status_code == 201
    assert response.json()["agent_name"] == demo.DEMO_AGENT_NAME


# ---- the migration ----


def test_migration_backfills_seeded_orders_and_leaves_real_ones_null(tmp_path):
    url = "sqlite:///" + str(tmp_path / "test.db")
    alembic_config = Config("alembic.ini")
    alembic_config.set_main_option("sqlalchemy.url", url)
    command.upgrade(alembic_config, "b91d6e2f4a73")

    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO mandates (id, buyer_label, raw_request, rules, max_price_inclusive) "
            "VALUES (1, 'seed-demo:clean: camping mug', 'x', '{}', 1), "
            "(2, 'Sam', 'x', '{}', 1)"
        ))
        for order_id, mandate_id in [(1, 1), (2, 1), (3, 1), (4, 2)]:
            connection.execute(text(
                "INSERT INTO orders (id, mandate_id, items, total, currency, status, watch_mode) "
                "VALUES (:id, :mandate_id, '[]', 1, 'USD', 'captured', 'normal')"
            ), {"id": order_id, "mandate_id": mandate_id})

    command.upgrade(alembic_config, "head")

    with engine.connect() as connection:
        rows = connection.execute(text("SELECT id, agent_name FROM orders ORDER BY id")).all()
    # seeded orders take turns like the seed does, the real one stays unknown
    assert [tuple(row) for row in rows] == [(1, "Wren"), (2, "Thistle"), (3, "Wren"), (4, None)]

    command.downgrade(alembic_config, "b91d6e2f4a73")
    with engine.connect() as connection:
        columns = connection.execute(text("PRAGMA table_info(orders)")).all()
    names = []
    for column in columns:
        names.append(column[1])
    assert "agent_name" not in names
