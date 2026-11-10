# Every test runs with known API keys, whatever is or isn't in the local .env

import pytest

from backend.app.config import get_settings
from backend.app.demo import reset_limiter
from backend.app.session import login_limiter
from keyed_client import AGENT_KEY, DEMO_PASSWORD, MERCHANT_KEY


@pytest.fixture(autouse=True)
def api_keys(monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", AGENT_KEY)
    # blank, so whatever agents a local .env names can't leak in. tests that need named agents
    # set their own
    monkeypatch.setenv("AGENT_API_KEYS", "")
    monkeypatch.setenv("MERCHANT_API_KEY", MERCHANT_KEY)
    monkeypatch.setenv("DEMO_MERCHANT_PASSWORD", DEMO_PASSWORD)
    get_settings.cache_clear()
    # the rate limiters are module-level, so their counts outlive a test. every TestClient comes
    # from the same address, so without this one test's logins or resets would count against the
    # next. cleared before and after, so nothing leaks either way. test_isolation.py checks it
    login_limiter.reset()
    reset_limiter.reset()
    yield
    get_settings.cache_clear()
    login_limiter.reset()
    reset_limiter.reset()
