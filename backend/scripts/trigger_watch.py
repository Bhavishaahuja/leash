# trigger_watch.py
# Drives the rubber-stamp watch on a running Leash API, end to end, with real Claude and sandbox
# PayPal:
#   a. approves 3 orders waiting for review, fast (800, 900, 700 ms), through real review links
#   b. checks GET /stats says the watch is on
#   c. places a wrong_color demo order on a Fernwood mandate, which should come back sharp
#   d. rejects one order slowly (4000 ms) and checks the watch is off again
#
# Run from the repo root, in cmd:
#   .venv\Scripts\python.exe backend\scripts\trigger_watch.py
#   .venv\Scripts\python.exe backend\scripts\trigger_watch.py --base-url https://your-api.example.com
#
# Needs MERCHANT_API_KEY in .env matching the API's, the API's DEMO_VAULT_ID set, at least 3 orders
# waiting for review, and a Fernwood mandate with an item and a color. seed_demo.py makes all that.
#
# Approving captures those holds and rejecting voids one, in the sandbox. Exits non-zero if any
# step fails.

import argparse
import os
import sys
import time

# make "backend.app" importable when run as a script
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

import requests  # noqa: E402

from backend.app.config import STORE_NAME, get_settings  # noqa: E402
from backend.app.intent.rules import normalize_merchant  # noqa: E402
from backend.app.rubber_stamp import STREAK_TO_TRIGGER  # noqa: E402
from backend.scripts.seed_demo import DEFAULT_BASE_URL, ApiError, SeedApi  # noqa: E402

FAST_APPROVALS_MS = [800, 900, 700]
SLOW_REJECT_MS = 4000
# the watch's own limit is 11 words, see SHARP_MAX_WORDS
SHARP_REASON_MAX_WORDS = 11


class WatchApi(SeedApi):
    # seed_demo's client with the merchant calls this script needs. it never uses the agent key

    def waiting_orders(self):
        # newest first, like GET /orders returns them
        response = self.call("GET", "/orders?limit=200", self.merchant)
        waiting = []
        for order in response.json():
            if order["status"] == "authorized" and order["review_state"] == "needs_review":
                waiting.append(order)
        return waiting

    def stats(self):
        return self.call("GET", "/stats", self.merchant).json()

    def mandates(self):
        return self.call("GET", "/mandates?limit=200", self.merchant).json()

    def demo_order(self, mandate_id, scenario):
        body = {"mandate_id": mandate_id, "scenario": scenario}
        return self.call("POST", "/demo/orders", self.merchant, body).json()

    def decide(self, order_id, action, decision_ms):
        # opened first, then really wait, so the server's own decision_ms (the one the watch
        # reads) is about the same as the number we send. the sent one is only kept to compare
        token = self.review_token(order_id)
        self.call("POST", "/review/" + token + "/opened", ok=(204,))
        time.sleep(decision_ms / 1000)
        body = {"decision_ms": decision_ms}
        return self.call("POST", "/review/" + token + "/" + action, body=body).json()


results = []


def report(step, passed, detail):
    results.append(passed)
    label = "PASS" if passed else "FAIL"
    print(label + "  " + step + ": " + detail, flush=True)


def print_stats(stats):
    print("  /stats:")
    for name in [
        "total_orders", "auto_captured", "needs_review", "approved", "voided",
        "median_decision_ms", "fast_streak", "rubber_stamp_active", "fast_threshold_ms",
        "recent_decision_ms",
    ]:
        print("    " + name + ": " + str(stats.get(name)))


def approve_fast(api):
    waiting = api.waiting_orders()
    print("  " + str(len(waiting)) + " orders waiting for review")
    if len(waiting) < len(FAST_APPROVALS_MS):
        report("a. fast approvals", False, "need " + str(len(FAST_APPROVALS_MS))
               + " orders in needs_review, found " + str(len(waiting)) + ". Run seed_demo.py")
        return

    approved = 0
    for order in waiting:
        if approved == len(FAST_APPROVALS_MS):
            break
        decision_ms = FAST_APPROVALS_MS[approved]
        try:
            decided = api.decide(order["id"], "approve", decision_ms)
        except ApiError as error:
            # an expired hold or a PayPal hiccup on one order shouldn't sink the run, try the next
            print("  order " + str(order["id"]) + " skipped: " + str(error))
            continue
        approved += 1
        print("  order " + str(order["id"]) + " approved in ~" + str(decision_ms) + " ms: "
              + decided["status"] + ", " + decided["review_state"])

    report("a. fast approvals", approved == len(FAST_APPROVALS_MS),
           str(approved) + " of " + str(len(FAST_APPROVALS_MS)) + " approved")


def check_watch_on(api):
    stats = api.stats()
    print_stats(stats)
    report("b. watch is on", stats["rubber_stamp_active"] is True,
           "rubber_stamp_active=" + str(stats["rubber_stamp_active"])
           + ", fast_streak=" + str(stats["fast_streak"])
           + " (needs " + str(STREAK_TO_TRIGGER) + ")")


def fernwood_mandate_with_color(api):
    # wrong_color needs an item and a color to get wrong, same refusals as build_demo_order
    for mandate in api.mandates():
        if normalize_merchant(mandate["merchant"]) not in fernwood_names():
            continue
        rules = mandate["rules"] or {}
        if str(rules.get("item") or "").strip() and str(rules.get("color") or "").strip():
            return mandate
    return None


def fernwood_names():
    # "Fernwood" and "Fernwood Supply Co." both count, like the merchant rule
    return [normalize_merchant(STORE_NAME), "fernwood", "fernwood supply"]


def place_wrong_color(api):
    mandate = fernwood_mandate_with_color(api)
    if mandate is None:
        report("c. sharp wrong_color order", False,
               "no Fernwood mandate with an item and a color. Run seed_demo.py")
        return None

    print("  mandate " + str(mandate["id"]) + ": " + mandate["raw_request"])
    order = api.demo_order(mandate["id"], "wrong_color")
    check = order.get("intent_check") or {}
    reason = check.get("reason") or ""
    print("  order " + str(order["id"]) + ": status=" + order["status"]
          + ", review_state=" + str(order["review_state"])
          + ", watch_mode=" + order["watch_mode"])
    print("  reason: " + reason)

    escalated = order["status"] == "authorized" and order["review_state"] == "needs_review"
    report("c. sharp wrong_color order", order["watch_mode"] == "sharp" and escalated,
           "watch_mode=" + order["watch_mode"] + ", review_state=" + str(order["review_state"]))

    word_count = len(reason.split())
    sharp_form = reason.startswith("Mandate said") and ", order has" in reason
    report("c. sharp reason", sharp_form and word_count <= SHARP_REASON_MAX_WORDS,
           str(word_count) + " words, " + ("reads" if sharp_form else "doesn't read")
           + " \"Mandate said X, order has Y\"")
    return order


def reject_slowly(api, preferred_order):
    # the wrong_color order from step c if it's waiting, so the run cleans up its own hold
    target = None
    waiting = api.waiting_orders()
    for order in waiting:
        if preferred_order is not None and order["id"] == preferred_order["id"]:
            target = order
    if target is None and waiting:
        target = waiting[0]
    if target is None:
        report("d. slow reject", False, "no order left in needs_review to reject")
        return

    decided = api.decide(target["id"], "reject", SLOW_REJECT_MS)
    print("  order " + str(target["id"]) + " rejected in ~" + str(SLOW_REJECT_MS) + " ms: "
          + decided["status"] + ", " + decided["review_state"])

    stats = api.stats()
    print_stats(stats)
    report("d. watch is off", stats["rubber_stamp_active"] is False and stats["fast_streak"] == 0,
           "rubber_stamp_active=" + str(stats["rubber_stamp_active"])
           + ", fast_streak=" + str(stats["fast_streak"]))


def main():
    # Windows consoles default to cp1252
    sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="Turn the rubber-stamp watch on and off again")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    args = parser.parse_args()

    settings = get_settings()
    if not settings.merchant_api_key:
        print("MERCHANT_API_KEY has to be in .env, matching the API's.")
        return 1

    # the agent key isn't needed for any of this, so it's left out
    api = WatchApi(requests.Session(), args.base_url, "", settings.merchant_api_key)
    print("Driving the rubber-stamp watch on " + api.base_url + "...")

    try:
        print("\na. Approve " + str(len(FAST_APPROVALS_MS)) + " waiting orders fast")
        approve_fast(api)
        print("\nb. Check the watch is on")
        check_watch_on(api)
        print("\nc. Place a wrong_color demo order")
        placed = place_wrong_color(api)
        print("\nd. Reject one order slowly")
        reject_slowly(api, placed)
    except (ApiError, requests.RequestException) as error:
        report("run", False, str(error))

    passed = 0
    for result in results:
        if result:
            passed += 1
    print("\n" + str(passed) + " of " + str(len(results)) + " checks passed")
    if passed == len(results):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
