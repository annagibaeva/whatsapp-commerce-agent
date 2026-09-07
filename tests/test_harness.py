from wca.cases import load_cases
from wca.escalation import Template, TemplateRegistry
from wca.harness import run_cases
from wca.rules.store import load_ruleset

CASES = load_cases("cases/v0.cases.json")
RULES = load_ruleset("policy/salon.rules.json")
REGISTRY = TemplateRegistry(templates=(
    Template(reason="under_16_needs_guardian", name="guardian_notice", approved=True),
    Template(reason="patch_test_first_colour", name="patch_test_notice", approved=True),
))


def test_with_the_gate_on_there_are_no_bad_bookings():
    report = run_cases(CASES, RULES, REGISTRY, gate_on=True)
    assert report.bad_bookings == 0


def test_with_the_gate_off_at_least_one_bad_booking_gets_through():
    # If removing the gate changes nothing, the gate is not doing anything.
    report = run_cases(CASES, RULES, REGISTRY, gate_on=False)
    assert report.bad_bookings > 0


def test_every_case_reaches_the_action_it_expects():
    report = run_cases(CASES, RULES, REGISTRY, gate_on=True)
    wrong = [o.case_id for o in report.outcomes if not o.action_matched]
    assert wrong == []


def test_every_case_reaches_the_verdict_it_expects():
    report = run_cases(CASES, RULES, REGISTRY, gate_on=True)
    wrong = [(o.case_id, o.verdict.check) for o in report.outcomes if not o.verdict_matched]
    assert wrong == []


def test_the_report_prints_counts_next_to_every_rate():
    text = run_cases(CASES, RULES, REGISTRY, gate_on=True).render()
    assert "bad bookings" in text
    assert "n=" in text
    assert "twenty cases" in text.lower() or "20 cases" in text


def test_the_report_separates_grounding_from_conclusion_blocks():
    report = run_cases(CASES, RULES, REGISTRY, gate_on=True)
    assert report.grounding_blocks > 0
    assert report.conclusion_blocks > 0
