# run_evals.py
# Scores Leash's intent check against the labeled cases in cases.json. Each case is a mandate and
# an order with one answer: should Leash auto-capture it or hold it for a human.
#
# It calls run_intent_check from backend/app/intent/check.py directly, then decides with
# should_escalate from rubber_stamp.py, the same function auto-decide uses. There's no database
# and no PayPal, so nothing gets held or captured for real. The modes:
#   rules_only     hard rules only. Claude is never called: anything that passes the rules is
#                  treated as a match, which shows what the rules catch on their own
#   normal         rules + Claude, the way an order is checked most of the time
#   sharp          rules + Claude with the rubber-stamp watch on: sharp reasons, and the app only
#                  holds risk 60 and up instead of the usual auto-capture threshold
#   sharp_prompt   sharp reasons with the normal threshold. sharp changes two things at once, this
#                  separates the prompt's effect from the threshold's
#
# Wrong auto-captures are the number that matters: money taken for an order the human didn't
# want. False holds are the cost on the other side, a human asked about a fine order.
#
# Run from the repo root, in cmd:
#   .venv\Scripts\python.exe backend\evals\run_evals.py                      all four modes
#   .venv\Scripts\python.exe backend\evals\run_evals.py --modes rules_only   no API calls
#   .venv\Scripts\python.exe backend\evals\run_evals.py --modes normal --cases model-01 qty-02
#
# The Claude modes need ANTHROPIC_API_KEY in .env, and make one call per case that passes the
# rules (43 of the 50 cases right now), so about 130 calls for all three. A full run (every
# mode, every case) saves to backend/evals/results.md. A partial run only prints, so it can't
# replace the full report with a slice of it, unless --out says where to save.

import argparse
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

# make "backend.app" importable when run as a script
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO_ROOT)

from pydantic import ValidationError  # noqa: E402

from backend.app import mandate_parser, rubber_stamp  # noqa: E402
from backend.app.config import CLAUDE_MODEL, get_settings  # noqa: E402
from backend.app.intent.check import build_decision, run_intent_check  # noqa: E402
from backend.app.models import Mandate, Order, Verdict  # noqa: E402
from backend.app.orders import OrderItemIn, item_to_json  # noqa: E402

EVALS_DIR = os.path.dirname(os.path.abspath(__file__))
CASES_FILE = os.path.join(EVALS_DIR, "cases.json")
RESULTS_FILE = os.path.join(EVALS_DIR, "results.md")

# the expiry rule compares against "now". pinned, so a case's mandate doesn't quietly expire
# between runs and turn a clean case into a hold
EVAL_NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)

CATEGORIES = [
    "clean", "near_miss_color", "just_under_cap", "wrong_size", "substitution", "wrong_model",
    "wrong_date", "wrong_quantity", "wrong_merchant", "injection",
]

# what the rules-only stand-in for Claude says about every order that reaches it
RULES_ONLY_REASON = "Not checked: rules only"


@dataclass(frozen=True)
class Mode:
    name: str
    label: str
    uses_claude: bool
    # sharp reasons from Claude and the hard rules
    sharp: bool
    # the rubber-stamp watch's threshold in should_escalate. the app always turns both on together
    watch_active: bool


MODES = [
    Mode("rules_only", "Rules only", uses_claude=False, sharp=False, watch_active=False),
    Mode("normal", "Rules + Claude", uses_claude=True, sharp=False, watch_active=False),
    Mode("sharp", "Rules + Claude, sharp", uses_claude=True, sharp=True, watch_active=True),
    Mode("sharp_prompt", "Rules + Claude, sharp reasons, normal threshold",
         uses_claude=True, sharp=True, watch_active=False),
]


class CaseError(Exception):
    pass


@dataclass
class CaseResult:
    case: dict
    auto_captured: bool
    verdict: str
    risk_score: int | None
    reason: str
    mismatched_fields: list
    checked_by: str
    latency_ms: int
    # Claude errored or timed out. the order still gets held, but that's not Claude catching it
    check_failed: bool

    @property
    def wrong_auto_capture(self):
        return self.auto_captured and not self.case["should_auto_capture"]

    @property
    def false_hold(self):
        return not self.auto_captured and self.case["should_auto_capture"]


# ---- loading cases ----


def load_cases(path=CASES_FILE):
    with open(path, encoding="utf-8") as cases_file:
        cases = json.load(cases_file)["cases"]

    seen_ids = set()
    for case in cases:
        if case["id"] in seen_ids:
            raise CaseError("Two cases are called " + case["id"])
        seen_ids.add(case["id"])
        if case["category"] not in CATEGORIES:
            raise CaseError(case["id"] + " has an unknown category: " + case["category"])
        # builds both, so a typo in any case fails before a single API call is spent
        build_mandate(case)
        build_order(case, 1)
    return cases


def build_mandate(case):
    # the same columns POST /mandates fills in from the parser's fields
    rules = dict(case["mandate"])
    return Mandate(
        buyer_label="eval:" + case["id"],
        raw_request=case["request"],
        rules=rules,
        max_price=mandate_parser.max_price_decimal(rules),
        max_price_inclusive=rules.get("max_price_inclusive") is not False,
        merchant=rules.get("merchant"),
        expires_at=mandate_parser.expires_at_datetime(rules),
    )


def build_order(case, order_id):
    # items go through OrderItemIn and item_to_json, so they're validated and saved exactly
    # the way POST /orders saves them
    order_data = case["order"]
    items_json = []
    items_total = Decimal("0")
    for raw_item in order_data["items"]:
        try:
            item = OrderItemIn(**raw_item)
        except ValidationError as error:
            raise CaseError(case["id"] + " has an item POST /orders would refuse") from error
        items_json.append(item_to_json(item))
        items_total += item.unit_price * item.quantity

    shipping = Decimal(order_data["shipping"])
    total = Decimal(order_data["total"])
    # the total is written out in the case so a reader sees it, and it has to add up
    if items_total + shipping != total:
        raise CaseError(
            case["id"] + " says its total is " + str(total) + " but the items and shipping come "
            "to " + str(items_total + shipping)
        )

    return Order(
        id=order_id,
        items=items_json,
        total=total,
        shipping_amount=shipping,
        currency=order_data.get("currency") or "USD",
        merchant=order_data.get("merchant"),
    )


# ---- running ----


class RulesOnlyClaude:
    # stands in for the Anthropic client in rules_only mode. anything that gets past the hard
    # rules is a clean match, so what's left is exactly what the rules would let through alone

    def __init__(self):
        self.calls = 0
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def with_options(self, **options):
        return self

    def _create(self, **kwargs):
        self.calls += 1
        tool_input = {"verdict": "match", "risk_score": 0, "reason": RULES_ONLY_REASON,
                      "mismatched_fields": []}
        block = SimpleNamespace(type="tool_use", name=kwargs["tools"][0]["name"], input=tool_input)
        return SimpleNamespace(stop_reason="tool_use", content=[block])


def run_case(case, order_id, mode, client):
    mandate = build_mandate(case)
    order = build_order(case, order_id)
    result = run_intent_check(mandate, order, client=client, now=EVAL_NOW, sharp=mode.sharp)

    # the Decision row the app would save, and the call auto_decide makes on it
    decision = build_decision(order, result)
    escalate = rubber_stamp.should_escalate(decision, mode.watch_active)

    return CaseResult(
        case=case,
        auto_captured=not escalate,
        verdict=result.verdict.value,
        risk_score=result.risk_score,
        reason=result.reason,
        mismatched_fields=list(result.mismatched_fields),
        checked_by=result.checked_by.value,
        latency_ms=result.latency_ms,
        check_failed=result.reason in rubber_stamp.CHECK_DID_NOT_RUN_REASONS,
    )


def run_mode(cases, mode, client=None, workers=4):
    # results come back in case order whatever order the threads finish in
    if not mode.uses_claude:
        client = RulesOnlyClaude()
        workers = 1
    elif client is None:
        client = mandate_parser.make_client(get_settings().anthropic_api_key)

    jobs = []
    for index, case in enumerate(cases):
        jobs.append((case, index + 1))

    if workers <= 1:
        results = []
        for case, order_id in jobs:
            results.append(run_case(case, order_id, mode, client))
        return results

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = []
        for case, order_id in jobs:
            futures.append(pool.submit(run_case, case, order_id, mode, client))
        results = []
        for future in futures:
            results.append(future.result())
    return results


# ---- scoring ----


@dataclass(frozen=True)
class Summary:
    cases: int
    mismatches: int
    caught: int
    wrong_auto_captures: int
    acceptable: int
    false_holds: int
    unsure: int
    check_failed: int
    avg_latency_ms: float


def summarize(results):
    mismatches = 0
    caught = 0
    wrong_auto_captures = 0
    acceptable = 0
    false_holds = 0
    unsure = 0
    check_failed = 0
    latency_total = 0
    for result in results:
        if result.case["should_auto_capture"]:
            acceptable += 1
            if result.false_hold:
                false_holds += 1
        else:
            mismatches += 1
            if result.wrong_auto_capture:
                wrong_auto_captures += 1
            else:
                caught += 1
        if result.verdict == Verdict.unsure.value:
            unsure += 1
        if result.check_failed:
            check_failed += 1
        latency_total += result.latency_ms

    average = latency_total / len(results) if results else 0.0
    return Summary(len(results), mismatches, caught, wrong_auto_captures, acceptable,
                   false_holds, unsure, check_failed, average)


# ---- the report ----


def percent(part, whole):
    if whole == 0:
        return "-"
    return str(round(100 * part / whole)) + "%"


def table_cell(text):
    # a pipe in a reason would split the markdown cell
    return str(text).replace("|", "\\|").replace("\n", " ")


def build_report(results_by_mode, ran_at=None):
    if ran_at is None:
        ran_at = datetime.now(timezone.utc)
    summaries = {}
    for mode, results in results_by_mode:
        summaries[mode.name] = summarize(results)

    lines = []
    lines.append("# Intent check evals")
    lines.append("")
    lines.append(
        "Ran " + ran_at.strftime("%Y-%m-%d %H:%M") + " UTC against " + CLAUDE_MODEL
        + ". Auto-capture threshold: risk under "
        + str(get_settings().auto_capture_max_risk) + " (60 with the watch on)."
    )
    lines.append("")

    # the headline. a held order costs a human a tap, a wrongly captured one costs the shopper
    lines.append("## Wrong auto-captures")
    lines.append("")
    for mode, _results in results_by_mode:
        summary = summaries[mode.name]
        lines.append(
            "- **" + mode.label + ": " + str(summary.wrong_auto_captures) + " of "
            + str(summary.mismatches) + "** orders the shopper didn't want were captured"
        )
    lines.append("")

    lines.append("## By mode")
    lines.append("")
    lines.append("| Mode | Wrong auto-captures | Caught | False holds | Unsure | Check failed "
                 "| Avg latency |")
    lines.append("|---|---|---|---|---|---|---|")
    for mode, _results in results_by_mode:
        summary = summaries[mode.name]
        lines.append(
            "| " + mode.label
            + " | **" + str(summary.wrong_auto_captures) + "**"
            + " | " + str(summary.caught) + " of " + str(summary.mismatches)
            + " | " + str(summary.false_holds) + " of " + str(summary.acceptable)
            + " | " + percent(summary.unsure, summary.cases)
            + " | " + str(summary.check_failed)
            + " | " + str(round(summary.avg_latency_ms)) + " ms |"
        )
    lines.append("")
    lines.append(
        "Caught counts any hold, so a check that failed and held by default still counts. "
        "Check failed says how many of those there were. Unsure is out of every case."
    )
    lines.append("")

    lines.append("## Right, by category")
    lines.append("")
    header = "| Category | Cases |"
    divider = "|---|---|"
    for mode, _results in results_by_mode:
        header += " " + mode.label + " |"
        divider += "---|"
    lines.append(header)
    lines.append(divider)
    for category in CATEGORIES:
        row = None
        for _mode, results in results_by_mode:
            in_category = []
            for result in results:
                if result.case["category"] == category:
                    in_category.append(result)
            if not in_category:
                continue
            right = 0
            for result in in_category:
                if not result.wrong_auto_capture and not result.false_hold:
                    right += 1
            if row is None:
                row = "| " + category + " | " + str(len(in_category)) + " |"
            row += " " + str(right) + " right |"
        if row is not None:
            lines.append(row)
    lines.append("")

    lines.append("## What went wrong")
    for mode, results in results_by_mode:
        lines.append("")
        lines.append("### " + mode.label)
        lines.append("")
        wrong = []
        for result in results:
            if result.wrong_auto_capture or result.false_hold:
                wrong.append(result)
        if not wrong:
            lines.append("Nothing. Every case landed where it should.")
            continue
        lines.append("| Case | Category | Went wrong | Verdict | Risk | Reason |")
        lines.append("|---|---|---|---|---|---|")
        for result in wrong:
            what = "auto-captured" if result.wrong_auto_capture else "held"
            lines.append(
                "| " + result.case["id"] + " | " + result.case["category"] + " | " + what
                + " | " + result.verdict + " | " + str(result.risk_score)
                + " | " + table_cell(result.reason) + " |"
            )
    lines.append("")
    return "\n".join(lines)


def main():
    # Windows consoles default to cp1252
    sys.stdout.reconfigure(encoding="utf-8")
    # ai_check logs a warning per failed call, which is worth seeing, not the per-request noise
    logging.basicConfig(level=logging.WARNING)

    mode_names = []
    for mode in MODES:
        mode_names.append(mode.name)

    parser = argparse.ArgumentParser(description="Score the intent check against cases.json")
    parser.add_argument("--modes", nargs="+", choices=mode_names, default=mode_names)
    parser.add_argument("--cases", nargs="+", metavar="ID", help="only these case ids")
    parser.add_argument("--out", help="where to save the report. default: results.md, "
                        "full runs only")
    parser.add_argument("--workers", type=int, default=4,
                        help="Claude calls at once, for the modes that make them")
    args = parser.parse_args()

    try:
        cases = load_cases()
    except CaseError as error:
        print("cases.json has a problem: " + str(error))
        return 1

    if args.cases:
        wanted = set(args.cases)
        picked = []
        unknown = set(wanted)
        for case in cases:
            if case["id"] in wanted:
                picked.append(case)
                unknown.discard(case["id"])
        if unknown:
            print("No such case: " + ", ".join(sorted(unknown)))
            return 1
        cases = picked

    modes = []
    for mode in MODES:
        if mode.name in args.modes:
            modes.append(mode)

    client = None
    needs_claude = any(mode.uses_claude for mode in modes)
    if needs_claude:
        if not get_settings().anthropic_api_key and not os.environ.get("ANTHROPIC_API_KEY"):
            print("The Claude modes need ANTHROPIC_API_KEY in .env. Try --modes rules_only.")
            return 1
        # one client for every call, like the app shares one across requests
        client = mandate_parser.make_client(get_settings().anthropic_api_key)

    results_by_mode = []
    for mode in modes:
        print("Running " + mode.label + " on " + str(len(cases)) + " cases...", flush=True)
        results_by_mode.append((mode, run_mode(cases, mode, client, args.workers)))

    report = build_report(results_by_mode)
    print()
    print(report)

    out_path = args.out
    full_run = len(modes) == len(MODES) and not args.cases
    if out_path is None and full_run:
        out_path = RESULTS_FILE
    if out_path is None:
        print("Partial run, not saved. Pass --out to keep it.")
        return 0
    with open(out_path, "w", encoding="utf-8") as out_file:
        out_file.write(report)
    print("Saved to " + out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
