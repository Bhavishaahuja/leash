# week1_e2e.py
# Week 1 end to end, against a running Leash API (local or Render):
#   1. create a mandate (Claude parses it)
#   2. order A with the saved vault id -> what the mandate asked for -> auto-captured, or
#      approved through a review link if Claude wanted a human to look
#   3. order B with the saved vault id -> not what it asked for -> needs review -> the merchant
#      creates a link -> the human opens it and rejects
#   4. the agent key gets a 401 on everything it shouldn't reach, its status route shows four
#      fields, the dashboard login works, and reusing a link fails
#   5. read both back from PayPal: A's authorization CAPTURED and its capture COMPLETED,
#      B's authorization VOIDED. then send A's capture again with the same PayPal-Request-Id,
#      which has to return the same capture and not take the money twice
#
# Run from the repo root:
#   python backend/scripts/week1_e2e.py                                   localhost:8000
#   python backend/scripts/week1_e2e.py https://leash-api-q3rw.onrender.com    Render
# or set LEASH_API_URL=https://leash-api-q3rw.onrender.com first and leave the argument off
#
# Needs spikes/.vault_token.json (run spikes/spike2_vault.py once) and the PayPal keys in
# your local .env, for the read-back, plus AGENT_API_KEY, MERCHANT_API_KEY and
# DEMO_MERCHANT_PASSWORD matching the API's.
# The API side needs its own keys (Render env vars).
# Money never really moves: it's sandbox, and order B is voided.

import json
import os
import sys
import time
from types import SimpleNamespace

# make "backend.app" importable when run as a script
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

import requests  # noqa: E402

from backend.app.config import get_settings  # noqa: E402
from backend.app.orders import CAPTURE, payment_request_id  # noqa: E402
from backend.app.paypal_client import client_from_settings  # noqa: E402

# the command-line base_url wins, then LEASH_API_URL, then the local API. straight from the
# environment, not .env, so a leftover .env line can't quietly send a local run to Render
DEFAULT_BASE_URL = os.environ.get("LEASH_API_URL", "").strip() or "http://127.0.0.1:8000"
VAULT_FILE = os.path.join(REPO_ROOT, "spikes", ".vault_token.json")

MANDATE = {
    "buyer_label": "week1-e2e",
    "request_text": "Get me black trail runners, women's size 9, and a pair of hiking socks, "
    "under $120 all in, from Fernwood Supply Co, sometime this week.",
    "merchant": "Fernwood Supply Co",
}

ORDER_A_ITEMS = [
    {"name": "Trail runner", "color": "black", "size": "W9", "quantity": 1,
     "unit_price": "89.00"},
    {"name": "Wool hiking socks", "quantity": 1, "unit_price": "14.00"},
]
# clearly not what the mandate asked for, so a human gets asked
ORDER_B_ITEMS = [
    {"name": "Leather hiking boot", "color": "brown", "size": "M11", "quantity": 1,
     "unit_price": "95.00"},
]

TAP = {"decision_ms": 4200}

AGENT = {"X-API-Key": get_settings().agent_api_key}
MERCHANT = {"X-API-Key": get_settings().merchant_api_key}

problems = []


def check(label, actual, expected):
    if actual == expected:
        print("  ok   " + label + ": " + str(actual))
    else:
        print("  FAIL " + label + ": got " + str(actual) + ", expected " + str(expected))
        problems.append(label)


def call(method, base_url, path, body=None, headers=None):
    response = requests.request(method, base_url + path, json=body, headers=headers, timeout=120)
    if response.status_code >= 400:
        print("  " + method + " " + path + " failed:", response.status_code, response.text)
        sys.exit(1)
    return response.json()


def wait_for_health(base_url):
    # a free Render service sleeps after 15 minutes idle, waking it takes about a minute
    print("Checking " + base_url + "/health ...")
    for attempt in range(1, 19):
        try:
            response = requests.get(base_url + "/health", timeout=30)
            if response.status_code == 200:
                print("  API is up:", response.json())
                return
            print("  attempt " + str(attempt) + ": " + str(response.status_code))
        except requests.RequestException as error:
            print("  attempt " + str(attempt) + ": " + type(error).__name__)
        time.sleep(10)
    print("API never came up.")
    sys.exit(1)


def place_order(base_url, mandate_id, vault_id, items, label):
    placed = call("POST", base_url, "/orders", {
        "mandate_id": mandate_id,
        "vault_id": vault_id,
        "items": items,
        "shipping": "5.00",
    }, AGENT)
    check(label + " agent response fields", sorted(placed),
          ["id", "review_state", "status", "total"])
    # the rest takes the merchant key
    order = call("GET", base_url, "/orders/" + str(placed["id"]), headers=MERCHANT)
    print("\n" + label + ": order " + str(order["id"]) + ", total $" + order["total"])
    print("  paypal order:", order["paypal_order_id"])
    print("  authorization:", order["authorization_id"])
    print("  expires:", order["authorization_expires_at"])
    check_result = order["intent_check"]
    print("  check:", check_result["checked_by"], check_result["verdict"],
          "risk", check_result["risk_score"], "|", check_result["reason"])
    print("  status:", order["status"], "| review_state:", order["review_state"])
    return order


def expect_status(method, base_url, path, headers, expected, label):
    # no call(), since an error is the right answer here
    response = requests.request(
        method, base_url + path, json=TAP, headers=headers, timeout=120
    )
    check(label, response.status_code, expected)


def review_link(base_url, order):
    path = "/orders/" + str(order["id"]) + "/review-link"
    return "/review/" + call("POST", base_url, path, headers=MERCHANT)["review_token"]


def main():
    base_url = DEFAULT_BASE_URL
    if len(sys.argv) == 2:
        base_url = sys.argv[1]
    elif len(sys.argv) > 2:
        print("Usage: python backend/scripts/week1_e2e.py [base_url]")
        sys.exit(1)
    base_url = base_url.rstrip("/")

    if not os.path.exists(VAULT_FILE):
        print("No " + VAULT_FILE + ". Run python spikes/spike2_vault.py once first.")
        sys.exit(1)
    with open(VAULT_FILE) as f:
        vault_id = json.load(f)["vault_id"]

    if (not AGENT["X-API-Key"] or not MERCHANT["X-API-Key"]
            or not get_settings().demo_merchant_password):
        print("AGENT_API_KEY, MERCHANT_API_KEY and DEMO_MERCHANT_PASSWORD have to be in .env, "
              "matching the API's.")
        sys.exit(1)

    wait_for_health(base_url)

    # 1. mandate
    mandate = call("POST", base_url, "/mandates", MANDATE, AGENT)
    print("\nMandate " + str(mandate["id"]) + " created")
    rules = mandate["rules"]
    print("  item:", rules.get("item"), "| max_price:", mandate["max_price"],
          "| merchant:", mandate["merchant"], "| expires:", mandate["expires_at"])

    # 2. order A: right items, so either captured already or a tap away
    order_a = place_order(base_url, mandate["id"], vault_id, ORDER_A_ITEMS, "Order A")
    a_path = "/orders/" + str(order_a["id"])
    if order_a["review_state"] == "needs_review":
        print("  Claude wanted a human to look, approving")
        a_link = review_link(base_url, order_a)
        call("GET", base_url, a_link)
        requests.post(base_url + a_link + "/opened", timeout=120)
        call("POST", base_url, a_link + "/approve", TAP)
        order_a = call("GET", base_url, a_path, headers=MERCHANT)
        check("Order A review_state", order_a["review_state"], "approved")
    else:
        check("Order A review_state", order_a["review_state"], "auto_approved")
    print("  capture:", order_a["capture_id"])
    check("Order A status", order_a["status"], "captured")

    # 3. order B: wrong items, has to stop for a human and no money moves until they say so
    order_b = place_order(base_url, mandate["id"], vault_id, ORDER_B_ITEMS, "Order B")
    b_path = "/orders/" + str(order_b["id"])
    check("Order B status after create", order_b["status"], "authorized")
    check("Order B review_state after create", order_b["review_state"], "needs_review")

    # the agent is holding a flagged order. it can't do anything about it
    print("\nThe agent trying to approve its own order...")
    expect_status("POST", base_url, b_path + "/review-link", AGENT, 401, "agent creating a link")
    expect_status("POST", base_url, b_path + "/approve", AGENT, 404, "agent on the old route")
    expect_status("POST", base_url, "/review/guess/approve", AGENT, 404, "agent guessing a token")
    expect_status("GET", base_url, "/orders", AGENT, 401, "agent reading orders")
    expect_status("GET", base_url, b_path, AGENT, 401, "agent reading an order")

    # all it gets to see is where the order stands
    status = call("GET", base_url, "/agent/orders/" + str(order_b["id"]), headers=AGENT)
    check("agent status fields", sorted(status), ["id", "review_state", "status", "total"])
    check("agent status review_state", status["review_state"], "needs_review")

    print("\nThe dashboard logging in...")
    expect_status("GET", base_url, "/orders", None, 401, "dashboard before logging in")
    session = call("POST", base_url, "/auth/session",
                   {"password": get_settings().demo_merchant_password})
    dashboard = {"Authorization": "Bearer " + session["session_token"]}
    print("  session expires:", session["expires_at"])
    expect_status("GET", base_url, "/orders", dashboard, 200, "dashboard reading orders")
    expect_status("POST", base_url, "/orders", dashboard, 401, "dashboard placing an order")

    print("\nThe human opening their link and rejecting...")
    b_path_link = "/orders/" + str(order_b["id"]) + "/review-link"
    b_link = "/review/" + call("POST", base_url, b_path_link, headers=dashboard)["review_token"]
    # what a chat app does to build a preview. it can't start the clock
    page = call("GET", base_url, b_link)
    print("  they see:", page["intent_check"]["reason"])
    time.sleep(1)
    opened = requests.post(base_url + b_link + "/opened", timeout=120)
    check("confirm page says it rendered", opened.status_code, 204)
    time.sleep(2)
    call("POST", base_url, b_link + "/reject", TAP)
    order_b = call("GET", base_url, b_path, headers=MERCHANT)
    check("Order B status after reject", order_b["status"], "voided")
    check("Order B review_state after reject", order_b["review_state"], "rejected")
    check("Order B void_result", order_b["void_result"], "VOIDED")
    check("Order B client_decision_ms", order_b["approval"]["client_decision_ms"], 4200)
    # the server's clock, not the 4200 the client said
    server_ms = order_b["approval"]["decision_ms"]
    # the 2 seconds after opening and not the 1 before it. a slow free-tier Render can add a lot
    check("Order B decision_ms is the server's own", 2000 <= server_ms < 30000, True)
    print("  decision_ms:", server_ms)

    # 4. the link is used up
    print("\nSecond taps...")
    expect_status("POST", base_url, b_link + "/approve", None, 409, "reusing a link")
    expect_status("GET", base_url, b_link, None, 410, "opening a used link")
    expect_status("POST", base_url, a_path + "/review-link", MERCHANT, 409, "link on a done order")

    # 5. trust PayPal's record, not ours
    print("\nReading back from PayPal...")
    paypal = client_from_settings(get_settings())
    auth_a = paypal.get_authorization(order_a["authorization_id"])
    check("PayPal: order A authorization", auth_a["status"], "CAPTURED")
    capture_a = paypal.get_capture(order_a["capture_id"])
    check("PayPal: order A capture", capture_a["status"], "COMPLETED")
    auth_b = paypal.get_authorization(order_b["authorization_id"])
    check("PayPal: order B authorization", auth_b["status"], "VOIDED")

    # the idempotency everything else leans on, against PayPal for real
    print("\nSending order A's capture again with the same PayPal-Request-Id...")
    request_id = payment_request_id(
        SimpleNamespace(id=order_a["id"], authorization_id=order_a["authorization_id"]), CAPTURE
    )
    replay = paypal.capture_authorization(order_a["authorization_id"], request_id=request_id)
    check("PayPal: replay returns the same capture", replay["id"], order_a["capture_id"])

    # and the API's own read side agrees
    detail = call("GET", base_url, a_path, headers=MERCHANT)
    check("GET order A mandate id", detail["mandate"]["id"], mandate["id"])
    newest = call("GET", base_url, "/orders?limit=2", headers=MERCHANT)
    check("GET /orders is newest first", newest[0]["id"], order_b["id"])

    print()
    if problems:
        print("Week 1 e2e FAILED: " + ", ".join(problems))
        sys.exit(1)
    print("Week 1 e2e passed: mandate, auto-decide, review link, API keys, dashboard login, "
          "replay, confirmed by PayPal.")


if __name__ == "__main__":
    main()
