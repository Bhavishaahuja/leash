# Who's allowed to call what. Demo-grade on purpose, see "Security model" in the README.
#   agent key:     what a shopping agent holds. it can create mandates and orders, nothing else.
#                  each agent has its own (AGENT_API_KEYS), and the key is who the agent is
#   merchant key:  what scripts hold. it reads orders and creates review links
#   session token: what the dashboard holds after logging in. it does what the merchant key does,
#                  so the key itself never has to reach a browser
#
# A human approving on their phone has none of these. They hold a review link, see review.py.

import hashlib
import hmac
import secrets
import time
from typing import Annotated

from fastapi import Depends, HTTPException
from fastapi.security import APIKeyHeader, HTTPBearer

from backend.app.config import agent_keys, get_settings

API_KEY_HEADER = "X-API-Key"
SESSION_LIFETIME_SECONDS = 8 * 60 * 60

# auto_error off so a missing key gets the same 401 as a wrong one, FastAPI would send a 403
api_key_header = APIKeyHeader(name=API_KEY_HEADER, auto_error=False)
bearer_header = HTTPBearer(auto_error=False)

ProvidedKey = Annotated[str | None, Depends(api_key_header)]
ProvidedBearer = Annotated[object, Depends(bearer_header)]


def check_key(provided, expected):
    # an unset key on the server has to lock the door, not leave it open: without this an empty
    # header would match an empty setting. compare_digest so timing doesn't leak the key
    if not expected or not provided:
        raise unauthorized()
    if not secrets.compare_digest(provided.encode(), expected.encode()):
        raise unauthorized()


def unauthorized():
    return HTTPException(status_code=401, detail="Missing or wrong API key")


def session_signature(expires_at):
    # signed with a key derived from the merchant key, not the password: a password can be short,
    # and anyone holding a session token could try guessing it offline if it was the signing key.
    # no merchant key means no signature, and so no sessions. changing that key logs everyone out
    merchant_key = get_settings().merchant_api_key
    if not merchant_key:
        return None
    signing_key = hmac.new(merchant_key.encode(), b"leash-session-v1", hashlib.sha256).digest()
    return hmac.new(signing_key, str(expires_at).encode(), hashlib.sha256).hexdigest()


def issue_session_token(now=None):
    # "<expiry>.<signature>". returns (token, expiry), or (None, None) if sessions aren't set up
    if now is None:
        now = time.time()
    expires_at = int(now) + SESSION_LIFETIME_SECONDS
    signature = session_signature(expires_at)
    if signature is None:
        return None, None
    return str(expires_at) + "." + signature, expires_at


def session_token_is_valid(token, now=None):
    if now is None:
        now = time.time()

    expires_text, _, signature = token.partition(".")
    # isascii too, since isdigit alone is true for digits from other alphabets
    if not expires_text.isascii() or not expires_text.isdigit():
        return False
    expected = session_signature(int(expires_text))
    if expected is None:
        return False
    # the signature comes first: the expiry means nothing until we know we wrote it
    if not secrets.compare_digest(signature.encode(), expected.encode()):
        return False
    return now < int(expires_text)


def require_agent(provided: ProvidedKey):
    # returns the agent's name. it comes from the key alone: whatever the request body says about
    # who sent it is ignored, so one agent can't put its orders under another's name
    return agent_name_for_key(provided, agent_keys(get_settings()))


def agent_name_for_key(provided, keys):
    # no agents configured means no agent gets in, same as an unset key in check_key
    if not provided or not keys:
        raise unauthorized()
    # every key gets compared, so how long this takes doesn't say which one nearly matched
    matched_name = None
    for key, name in keys.items():
        if secrets.compare_digest(provided.encode(), key.encode()):
            matched_name = name
    if matched_name is None:
        raise unauthorized()
    return matched_name


# for a route that needs to know which agent is calling, not just that one is
AgentName = Annotated[str, Depends(require_agent)]


def require_merchant(provided: ProvidedKey, bearer: ProvidedBearer):
    if bearer is not None and session_token_is_valid(bearer.credentials):
        return
    check_key(provided, get_settings().merchant_api_key)
