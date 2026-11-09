# The eval harness in backend/evals. CI runs the rules-only mode, which never calls Claude, so the
# cases and the scoring stay honest without an API key. The Claude modes are only run by hand.

import json
from types import SimpleNamespace

import pytest

from backend.app import mandate_parser
from backend.evals import run_evals

RULES_ONLY = run_evals.MODES[0]


@pytest.fixture(scope="module")
def cases():
    return run_evals.load_cases()


@pytest.fixture
def no_real_claude(monkeypatch):
    # if anything in here tried to build a real Anthropic client, the test fails instead of
    # quietly spending money (or hanging without a key)
    def refuse(api_key):
        raise AssertionError("an eval test tried to call the real Claude")

    monkeypatch.setattr(mandate_parser, "make_client", refuse)


# ---- the cases themselves ----


def test_fifty_cases_across_every_category(cases):
    assert len(cases) == 50
    categories = set()
    for case in cases:
        categories.add(case["category"])
    assert categories == set(run_evals.CATEGORIES)


def test_about_fifteen_cases_should_capture_so_false_holds_get_measured(cases):
    acceptable = 0
    for case in cases:
        if case["should_auto_capture"]:
            acceptable += 1
    assert 12 <= acceptable <= 18


def test_clean_cases_capture_and_the_broken_categories_dont(cases):
    # near misses and the cap's edges go both ways on purpose, the rest have one answer
    always_capture = ["clean"]
    never_capture = ["wrong_size", "substitution", "wrong_model", "wrong_date", "wrong_quantity",
                     "wrong_merchant", "injection"]
    for case in cases:
        if case["category"] in always_capture:
            assert case["should_auto_capture"] is True, case["id"]
        if case["category"] in never_capture:
            assert case["should_auto_capture"] is False, case["id"]


def test_every_case_explains_its_label(cases):
    for case in cases:
        assert case["note"].strip(), case["id"]
        assert case["request"].strip(), case["id"]


def test_a_case_whose_total_doesnt_add_up_is_refused(cases):
    broken = json.loads(json.dumps(cases[0]))
    broken["order"]["total"] = "1.00"

    with pytest.raises(run_evals.CaseError, match="items and shipping come to"):
        run_evals.build_order(broken, 1)


def test_a_case_with_an_item_the_api_would_refuse_is_refused(cases):
    broken = json.loads(json.dumps(cases[0]))
    broken["order"]["items"][0]["quantity"] = 0

    with pytest.raises(run_evals.CaseError, match="POST /orders would refuse"):
        run_evals.build_order(broken, 1)


# ---- rules only ----


def test_rules_only_holds_exactly_what_the_rules_should_catch(cases, no_real_claude):
    results = run_evals.run_mode(cases, RULES_ONLY)

    held = set()
    for result in results:
        if not result.auto_captured:
            held.add(result.case["id"])
    should_be_caught = set()
    for case in cases:
        if case.get("caught_by_rules"):
            should_be_caught.add(case["id"])
    assert held == should_be_caught


def test_rules_only_never_holds_a_fine_order(cases, no_real_claude):
    # a hard rule refusing a clean order would be a bug in the rules, not a judgment call
    summary = run_evals.summarize(run_evals.run_mode(cases, RULES_ONLY))

    assert summary.false_holds == 0
    assert summary.unsure == 0
    assert summary.check_failed == 0


def test_rules_only_checked_by_rules_when_they_fail(cases, no_real_claude):
    for result in run_evals.run_mode(cases, RULES_ONLY):
        if result.case.get("caught_by_rules"):
            assert result.checked_by == "rules", result.case["id"]
            assert result.verdict == "mismatch", result.case["id"]


def test_rules_only_summary_counts_add_up(cases, no_real_claude):
    summary = run_evals.summarize(run_evals.run_mode(cases, RULES_ONLY))

    assert summary.cases == 50
    assert summary.mismatches + summary.acceptable == 50
    assert summary.caught + summary.wrong_auto_captures == summary.mismatches


# ---- the modes use the app's auto-decide ----


class FixedClaude:
    # answers every check the same way, to see what each mode's auto-decide does with it
    def __init__(self, verdict, risk_score):
        self.answer = {"verdict": verdict, "risk_score": risk_score, "reason": "Fixed answer.",
                       "mismatched_fields": []}
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def with_options(self, **options):
        return self

    def _create(self, **kwargs):
        block = SimpleNamespace(type="tool_use", name=kwargs["tools"][0]["name"], input=self.answer)
        return SimpleNamespace(stop_reason="tool_use", content=[block])


def mode_named(name):
    for mode in run_evals.MODES:
        if mode.name == name:
            return mode
    raise AssertionError("no mode " + name)


def test_the_watch_threshold_lets_a_middling_unsure_through(cases):
    # unsure at 45: normal mode asks a human, the watch's 60 threshold doesn't. that's the app's
    # should_escalate, and the reason sharp and sharp_prompt are separate modes
    clean = []
    for case in cases:
        if case["category"] == "clean":
            clean.append(case)
    claude = FixedClaude("unsure", 45)

    normal = run_evals.run_mode(clean, mode_named("normal"), claude, workers=1)
    sharp = run_evals.run_mode(clean, mode_named("sharp"), claude, workers=1)
    sharp_prompt = run_evals.run_mode(clean, mode_named("sharp_prompt"), claude, workers=1)

    for result in normal + sharp_prompt:
        assert result.auto_captured is False
    for result in sharp:
        assert result.auto_captured is True


def test_a_mismatch_is_held_in_every_mode(cases):
    claude = FixedClaude("mismatch", 20)
    for mode in run_evals.MODES[1:]:
        for result in run_evals.run_mode(cases[:3], mode, claude, workers=2):
            assert result.auto_captured is False


def test_threads_keep_results_in_case_order(cases):
    results = run_evals.run_mode(cases, mode_named("normal"), FixedClaude("match", 5), workers=4)

    ids = []
    for result in results:
        ids.append(result.case["id"])
    expected = []
    for case in cases:
        expected.append(case["id"])
    assert ids == expected


# ---- the report ----


def test_report_leads_with_wrong_auto_captures(cases, no_real_claude):
    results = run_evals.run_mode(cases, RULES_ONLY)
    summary = run_evals.summarize(results)

    report = run_evals.build_report([(RULES_ONLY, results)])

    headline = report.index("## Wrong auto-captures")
    assert headline < report.index("## By mode")
    expected = ("**Rules only: " + str(summary.wrong_auto_captures) + " of "
                + str(summary.mismatches) + "**")
    assert expected in report
    assert "| Rules only | **" + str(summary.wrong_auto_captures) + "** |" in report


def test_report_lists_every_wrong_case_by_id(cases, no_real_claude):
    results = run_evals.run_mode(cases, RULES_ONLY)

    report = run_evals.build_report([(RULES_ONLY, results)])

    for result in results:
        if result.wrong_auto_capture:
            assert "| " + result.case["id"] + " |" in report


def test_a_pipe_in_a_reason_doesnt_break_the_table():
    assert run_evals.table_cell("a | b") == "a \\| b"
