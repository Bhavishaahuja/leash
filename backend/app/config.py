# settings come from env vars (or .env locally). names match .env.example

import logging
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# the one fictional store Leash runs for. not env vars, these never change between deploys
STORE_NAME = "Fernwood Supply Co"
# what shoppers actually call it. "from Fernwood" shouldn't send a clean order to review
STORE_ALIASES = ["fernwood", "fernwood supply", "fernwood supply co"]

# every Claude call in the app uses this, so a model bump only happens here
CLAUDE_MODEL = "claude-sonnet-5-5"


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
