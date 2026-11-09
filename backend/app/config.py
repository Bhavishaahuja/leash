# settings come from env vars (or .env locally). names match .env.example

import json
import logging
from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# the one fictional store Leash runs for. not env vars, these never change between deploys
STORE_NAME = "Fernwood Supply Co"
# what shoppers actually call it. "from Fernwood" shouldn't send a clean order to review
STORE_ALIASES = ["fernwood", "fernwood supply", "fernwood supply co"]

# every Claude call in the app uses this, so a model bump only happens here
CLAUDE_MODEL = "claude-sonnet-5-5"

# what an order placed with the old single AGENT_API_KEY is credited to. that key keeps working so
# a deploy doesn't lock the live agent out before AGENT_API_KEYS is set, see agent_keys
DEFAULT_AGENT_NAME = "Shopping agent"
# fits Order.agent_name
MAX_AGENT_NAME_LENGTH = 100


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # PayPal sandbox app
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_base_url: str = "https://api-m.sandbox.paypal.com"
    paypal_webhook_id: str = ""
    # where PayPal sends the buyer after approving (or cancelling) an order with no vault id.
    # placeholders until the confirm page exists
    paypal_return_url: str = "https://example.com/return"
    paypal_cancel_url: str = "https://example.com/cancel"

    # sandbox logins, only used by scripts and the demo
    sandbox_merchant_email: str = ""
    sandbox_buyer_email: str = ""

    anthropic_api_key: str = ""

    # sent as the X-API-Key header. left empty, that whole side of the API just returns 401
    agent_api_key: str = ""
    merchant_api_key: str = ""
    # one key per shopping agent, and the name its orders show up under. either a comma list,
    # key:Name,key:Name, or a JSON object, {"key": "Name"}. the name always comes from here and
    # never from what the agent sends, see auth.require_agent
    agent_api_keys: str = ""
    # the dashboard's login. left empty, login is off and the dashboard can't get a session
    demo_merchant_password: str = ""
    # the saved sandbox wallet POST /demo/orders pays with, from spikes/.vault_token.json. without
    # one PayPal waits on a buyer, so there's no hold and nothing to check. left empty, that's a 503
    demo_vault_id: str = ""

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/leash"

    # which sites' browsers may call the API, comma-separated. never a wildcard, see cors_origins
    frontend_origins: str = "http://localhost:5173"

    # a match only captures on its own below this risk score. at or above it, a human decides
    auto_capture_max_risk: int = 30

    @field_validator("agent_api_keys")
    @classmethod
    def agent_api_keys_must_parse(cls, value):
        # a typo here should stop the API starting, not quietly lock an agent out
        parse_agent_api_keys(value)
        return value

    @model_validator(mode="after")
    def agent_keys_arent_the_merchant_key(self):
        # an agent holding the merchant key could read every order and every reason it was flagged
        if self.merchant_api_key and self.merchant_api_key in agent_keys(self):
            raise ValueError("An agent key can't be the same as MERCHANT_API_KEY")
        return self

    @field_validator("database_url")
    @classmethod
    def use_psycopg_driver(cls, url):
        # Render hands out postgres:// or postgresql:// URLs. SQLAlchemy reads those as
        # psycopg2, but we install psycopg (v3), so point it at the right driver
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        if url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        return url


def parse_agent_api_keys(setting):
    # AGENT_API_KEYS as {key: name}. raises ValueError on anything it can't read for sure
    text = setting.strip()
    if not text:
        return {}

    pairs = []
    # "[" too, so a JSON list gets told it should be an object, not that it's missing a colon
    if text.startswith("{") or text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError("AGENT_API_KEYS isn't valid JSON") from error
        if not isinstance(parsed, dict):
            raise ValueError("AGENT_API_KEYS JSON has to be an object of key to name")
        for key, name in parsed.items():
            pairs.append((key, name))
    else:
        for entry in text.split(","):
            if not entry.strip():
                continue
            key, separator, name = entry.partition(":")
            if not separator:
                raise ValueError("Each AGENT_API_KEYS entry has to be key:Name")
            pairs.append((key, name))

    keys = {}
    for key, name in pairs:
        if not isinstance(key, str) or not isinstance(name, str):
            raise ValueError("AGENT_API_KEYS keys and names have to be text")
        key = key.strip()
        name = " ".join(name.split())
        if not key or not name:
            raise ValueError("Every agent in AGENT_API_KEYS needs a key and a name")
        if len(name) > MAX_AGENT_NAME_LENGTH:
            raise ValueError("Agent name too long: " + name[:20] + "...")
        if key in keys:
            # two names for one key would make the name depend on which entry came last
            raise ValueError("The same key is in AGENT_API_KEYS twice")
        keys[key] = name
    return keys


def agent_keys(settings):
    # every agent key the API accepts, with the name its orders are credited to. the old single
    # AGENT_API_KEY stays in as DEFAULT_AGENT_NAME unless AGENT_API_KEYS names it itself
    keys = parse_agent_api_keys(settings.agent_api_keys)
    legacy_key = settings.agent_api_key.strip()
    if legacy_key and legacy_key not in keys:
        keys[legacy_key] = DEFAULT_AGENT_NAME
    return keys


def cors_origins(setting):
    # browsers send the origin with no trailing slash, so one left in here would never match.
    # a wildcard gets dropped: a mistyped env var shouldn't open the API to every site
    origins = []
    for origin in setting.split(","):
        origin = origin.strip().rstrip("/")
        if origin == "*":
            logger.warning("Ignoring * in FRONTEND_ORIGINS, list each site instead")
        elif origin:
            origins.append(origin)
    return origins


@lru_cache
def get_settings():
    return Settings()
