"""The gate. Six checks. It can only say no.

The gate calls no model. It takes read-only views of the calendar and the
window rather than the objects themselves, so it cannot change anything
even by accident.

Checks run in order and stop at the first failure. Every block says which
check failed and which kind of mistake it was. Grounding means the agent
used a rule that does not exist or does not apply. Conclusion means the
rules were right and the call was still wrong.
"""

from __future__ import annotations

from typing import Any

from wca.models import BlockKind, GateCheck, Proposal, Verdict
from wca.rules.evaluate import Tri, evaluate_rule, missing_facts
from wca.rules.schema import RuleSet
from wca.rules.specificity import is_more_specific, matching_rules, unknown_rules

#: Outcomes that let a booking go ahead.
ALLOWS_BOOKING = {"allow", "require_deposit"}


def evaluate(
    proposal: Proposal,
    ruleset: RuleSet,
    calendar_view: dict[str, Any],
    window_view: dict[str, Any],
) -> Verdict:
    facts = proposal.facts

    # 1. Every cited rule exists at the version cited.
    cited = []
    for ref in proposal.cited_rules:
        rule = ruleset.get(ref.rule_id, ref.version)
        if rule is None:
            return Verdict.blocked(
                GateCheck.RULES_EXIST,
                BlockKind.GROUNDING,
                f"{ref.ref()} is not in this ruleset",
            )
        cited.append(rule)

    # 2. The facts support every cited rule.
    for rule in cited:
        result = evaluate_rule(rule, facts)
        if result is not Tri.TRUE:
            return Verdict.blocked(
                GateCheck.FACTS_SUPPORT,
                BlockKind.GROUNDING,
                f"{rule.ref()} evaluates {result.value} on these facts",
            )

    # 3. No more specific rule was left out, and nothing relevant is unknown.
    cited_ids = {r.id for r in cited}
    for rule in matching_rules(ruleset, facts):
        if rule.id in cited_ids:
            continue
        if any(is_more_specific(rule, c) for c in cited):
            return Verdict.blocked(
                GateCheck.OVERRIDE_MISSED,
                BlockKind.GROUNDING,
                f"{rule.ref()} also applies and is more specific: {rule.outcome.reason}",
            )

    for rule in unknown_rules(ruleset, facts):
        if rule.id in cited_ids:
            continue
        if any(is_more_specific(rule, c) for c in cited):
            gaps = ", ".join(missing_facts(rule, facts))
            return Verdict.blocked(
                GateCheck.OVERRIDE_MISSED,
                BlockKind.GROUNDING,
                f"{rule.ref()} might apply but we never established {gaps}",
            )

    # 4. A booking cites at least one rule that permits it.
    if proposal.action.type == "book":
        permitting = [r for r in cited if r.outcome.type in ALLOWS_BOOKING]
        if not permitting:
            return Verdict.blocked(
                GateCheck.BOOKING_CITES_RULE,
                BlockKind.GROUNDING,
                "no cited rule allows a booking",
            )

        # 5. The slot is still ours.
        if not calendar_view.get("slot_exists"):
            return Verdict.blocked(
                GateCheck.SLOT_STILL_FREE, BlockKind.CONCLUSION, "the slot does not exist"
            )
        if calendar_view.get("booked"):
            return Verdict.blocked(
                GateCheck.SLOT_STILL_FREE, BlockKind.CONCLUSION, "the slot is already booked"
            )
        if calendar_view.get("held_by_thread") != proposal.thread_id:
            holder = calendar_view.get("held_by_thread")
            detail = "the hold is gone" if holder is None else f"another thread holds it: {holder}"
            return Verdict.blocked(
                GateCheck.SLOT_STILL_FREE, BlockKind.CONCLUSION, detail
            )

    # 6. An escalation can still reach a human.
    if proposal.action.type == "escalate" and not window_view.get("deliverable"):
        return Verdict.blocked(
            GateCheck.ESCALATION_DELIVERABLE,
            BlockKind.CONCLUSION,
            window_view.get("why_not", "the escalation cannot be delivered"),
        )

    return Verdict.passed()
