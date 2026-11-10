# Rate limiter counts live in module-level objects, so they'd carry from one test to the next if
# conftest.py didn't clear them. The first run of this fills both to the limit, the second checks
# it starts with nothing. pytest runs parameters in order, so the second always comes after.

import pytest

from backend.app.demo import RESET_MAX_TRIES, reset_limiter
from backend.app.session import MAX_TRIES, login_limiter

ADDRESS = "testclient"


@pytest.mark.parametrize("step", ["fill", "check"])
def test_rate_limiters_start_every_test_clean(step):
    if step == "fill":
        for _ in range(MAX_TRIES):
            login_limiter.seconds_until_allowed(ADDRESS)
        for _ in range(RESET_MAX_TRIES):
            reset_limiter.seconds_until_allowed(ADDRESS)
        # really full, or the check below proves nothing
        assert login_limiter.seconds_until_allowed(ADDRESS) > 0
        assert reset_limiter.seconds_until_allowed(ADDRESS) > 0
    else:
        assert login_limiter.tries_by_address == {}
        assert reset_limiter.tries_by_address == {}
