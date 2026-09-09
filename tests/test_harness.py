from unittest.mock import patch

from wca.calendar.mock import MockCalendar
from wca.cases import load_cases
from wca.escalation import Template, TemplateRegistry
from wca.harness import run_cases
from wca.rules.render import to_english
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


def test_the_harness_actually_commits_every_booking_it_counts():
    # "bookings completed" must reflect a real MockCalendar.commit call,
    # not just a passing verdict on a book action. Spy on commit itself
    # so this fails if run_cases ever goes back to counting verdicts.
    calls: list[tuple[str, str]] = []
    original_commit = MockCalendar.commit

    def spy_commit(self, hold_id, idempotency_key, now):
        calls.append((hold_id, idempotency_key))
        return original_commit(self, hold_id, idempotency_key, now)

    with patch.object(MockCalendar, "commit", spy_commit):
        report = run_cases(CASES, RULES, REGISTRY, gate_on=True)

    assert report.booked > 0
    assert len(calls) == report.booked


def test_every_case_gets_an_audit_record():
    report = run_cases(CASES, RULES, REGISTRY, gate_on=True)
    assert len(report.audit_records) == len(CASES.cases)

    for record in report.audit_records:
        assert record.ruleset_version == RULES.ruleset_version

        cited = [RULES.get(r.rule_id, r.version) for r in record.proposal.cited_rules]
        cited = [r for r in cited if r is not None]

        # Every cited rule is nameable as rule_id@version.
        for ref in record.proposal.cited_rules:
            assert ref.ref() == f"{ref.rule_id}@{ref.version}"

        # The rules appear in English via rules.render.to_english, one
        # entry per cited rule, in order.
        assert record.rules_english == tuple(to_english(r.condition) for r in cited)


# --- the four KPIs the report has to publish ---------------------------
#
# The PRD sets six targets and the harness used to print two of them. The
# labels for the rest were already in the case file; only the arithmetic
# was missing. These tests pin the arithmetic, not the current numbers --
# a test asserting "100%" would have to be edited every time a case is
# added, which is how a suite stops being able to fail.

def test_booking_rate_counts_only_the_cases_meant_to_be_booked():
    """Denominator is bookable cases, not all twenty.

    Most of the twenty are meant to be refused. Dividing by twenty
    reports 20% for an agent that booked everything it should have.
    """
    report = run_cases(CASES, RULES, REGISTRY, gate_on=True)

    expected = [c.id for c in CASES.cases if c.expect_action == "book" and c.expect_allowed]
    assert report.bookable == len(expected)
    assert report.bookable < report.total, "if every case were bookable this test proves nothing"
    assert report.bookable_booked <= report.bookable


def test_escalation_precision_counts_only_escalations_the_agent_raised():
    """The counter-metric to bad bookings.

    Zero bad bookings is trivially achieved by escalating everything, so
    the denominator has to be what the agent escalated, not what it
    should have.
    """
    report = run_cases(CASES, RULES, REGISTRY, gate_on=True)

    assert report.escalated == sum(1 for o in report.outcomes if o.action == "escalate")
    assert report.escalated_correctly <= report.escalated


def test_cost_of_control_is_the_bookable_cases_the_gate_stopped():
    report = run_cases(CASES, RULES, REGISTRY, gate_on=True)
    assert report.cost_of_control == report.bookable - report.bookable_booked


def test_the_gate_can_only_cost_bookings_never_win_them():
    """Turning the gate on cannot book something turning it off did not.

    The gate only blocks. If gate-on ever completes a bookable case that
    gate-off missed, the gate is supplying an outcome rather than
    withholding one, and it has stopped being a control.
    """
    on = run_cases(CASES, RULES, REGISTRY, gate_on=True)
    off = run_cases(CASES, RULES, REGISTRY, gate_on=False)

    booked_on = {o.case_id for o in on.outcomes if o.booked}
    booked_off = {o.case_id for o in off.outcomes if o.booked}
    assert booked_on <= booked_off
    assert on.cost_of_control >= off.cost_of_control


def test_the_report_publishes_every_kpi_the_prd_names():
    """Section 7 of the PRD lists six. Five are printed; deliverability is
    enforced by gate check 6 rather than counted, since no escalation can
    pass without it."""
    text = run_cases(CASES, RULES, REGISTRY, gate_on=True).render()

    for label in ("bad bookings", "booking rate", "escalation precision", "cost of control"):
        assert label in text, f"{label} missing from the report"
    # Counts lead, percentages follow -- never a bare percentage.
    assert "/" in text and "%" in text
