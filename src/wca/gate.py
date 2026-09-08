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
from wca.rules.schema import Rule, RuleSet
from wca.rules.specificity import is_more_specific, matching_rules, unknown_rules

#: Outcomes that let a booking go ahead.
ALLOWS_BOOKING = {"allow", "require_deposit"}


def _true_of_type(ruleset: RuleSet, facts: dict[str, Any], outcome_type: str) -> tuple[Rule, ...]:
    """Every rule of one outcome type that evaluates TRUE on these facts.

    Cited or not. A veto rule does not care what the agent quoted.
    """
    return tuple(r for r in matching_rules(ruleset, facts) if r.outcome.type == outcome_type)


def _unknown_of_type(ruleset: RuleSet, facts: dict[str, Any], outcome_type: str) -> tuple[Rule, ...]:
    """Every rule of one outcome type whose truth we cannot yet establish.

    Cited or not, same as above. An unasked question must not let a veto
    be dodged for free.
    """
    return tuple(r for r in unknown_rules(ruleset, facts) if r.outcome.type == outcome_type)


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

    # 4. The rules must actually permit *this* booking. That takes all of:
    #
    #   a. at least one CITED rule permits it (allow or require_deposit).
    #   b. no rule anywhere in the ruleset that evaluates TRUE on these
    #      facts has outcome deny. Deny is an absolute veto: it does not
    #      matter whether the agent cited it, because a booking the
    #      policy forbids is forbidden regardless of what got quoted in
    #      support of it. This is what closes the hole where citing a
    #      narrower subset of rules dodges a deny that plainly applies —
    #      checking every rule, not only the cited ones, is the point.
    #   c. no rule anywhere in the ruleset that evaluates TRUE on these
    #      facts has outcome require_escalation. A booking that should
    #      have gone to a human is not a booking either, cited or not.
    #   d. no rule anywhere in the ruleset that evaluates TRUE on these
    #      facts has an unmet require_lead_time. Same reasoning as (b):
    #      citing deposit_over_threshold must not let a first-time colour
    #      client dodge the 48-hour patch test that patch_test_first_colour
    #      would otherwise require. It does not matter that the agent
    #      cited a different, also-true rule instead.
    #
    # (b), (c) and (d) all veto on UNKNOWN as well as TRUE. A deny,
    # require_escalation or require_lead_time rule whose facts were never
    # established is not a "no" the way a missing fact never is anywhere
    # else in this module (see check 3, and rules/evaluate.py's own
    # docstring on why UNKNOWN must not collapse to FALSE) — but it is
    # also not a proven "yes", and the veto only needs proof that the rule
    # does NOT apply, which an unestablished fact cannot supply. Skipping
    # unknowns here would let an agent dodge the veto for free by simply
    # never asking the question a rule depends on (never asking the
    # weekday, never asking the customer's age, never asking whether this
    # is a first colour visit), which is worse than citing a narrow
    # subset: it is not naming a fact at all.
    #
    # (b), (c) and (d) share the same shape: find every TRUE rule of the
    # outcome type, then every UNKNOWN one, cited or not. _true_of_type
    # and _unknown_of_type above are that one idea, factored out so the
    # three read as one veto pattern rather than three loops that happen
    # to look similar.
    #
    # (b) and (c) is why the load-time ranking guard that used to live in
    # rules/specificity.py (and ran from rules/store.py) was removed: it
    # treated deny as conflicting with a permit at equal priority, but the
    # gate itself vetoes on any true deny, unconditionally, so there was
    # nothing left for a priority to rank. See the comment in
    # rules/store.py for the removal itself.
    #
    # require_deposit is deliberately not enforced here. The PRD says v0
    # evaluates the deposit rule and stops short of collecting a deposit.
    # That gap is a scope decision, not something to "fix" later without
    # checking the PRD again.
    if proposal.action.type == "book":
        permitting = [r for r in cited if r.outcome.type in ALLOWS_BOOKING]
        if not permitting:
            return Verdict.blocked(
                GateCheck.BOOKING_CITES_RULE,
                BlockKind.GROUNDING,
                "no cited rule allows a booking",
            )

        for rule in _true_of_type(ruleset, facts, "deny"):
            return Verdict.blocked(
                GateCheck.BOOKING_CITES_RULE,
                BlockKind.GROUNDING,
                f"{rule.ref()} denies this booking: {rule.outcome.reason}",
            )

        for rule in _unknown_of_type(ruleset, facts, "deny"):
            gaps = ", ".join(missing_facts(rule, facts))
            return Verdict.blocked(
                GateCheck.BOOKING_CITES_RULE,
                BlockKind.GROUNDING,
                f"{rule.ref()} might deny this booking and we never established {gaps}",
            )

        for rule in _true_of_type(ruleset, facts, "require_escalation"):
            return Verdict.blocked(
                GateCheck.BOOKING_CITES_RULE,
                BlockKind.GROUNDING,
                f"{rule.ref()} requires escalation to a human: {rule.outcome.reason}",
            )

        for rule in _unknown_of_type(ruleset, facts, "require_escalation"):
            gaps = ", ".join(missing_facts(rule, facts))
            return Verdict.blocked(
                GateCheck.BOOKING_CITES_RULE,
                BlockKind.GROUNDING,
                f"{rule.ref()} might require escalation to a human and we never "
                f"established {gaps}",
            )

        for rule in _true_of_type(ruleset, facts, "require_lead_time"):
            if rule.outcome.hours is None:
                continue
            needed = rule.outcome.hours
            if "hours_until_appointment" not in facts:
                return Verdict.blocked(
                    GateCheck.BOOKING_CITES_RULE,
                    BlockKind.GROUNDING,
                    f"{rule.ref()} requires {needed} hours lead time and we never "
                    "established hours_until_appointment",
                )
            available = facts["hours_until_appointment"]
            try:
                unmet = available < needed
            except TypeError:
                # A non-numeric hours_until_appointment (bad upstream data,
                # not a policy question) is a fact we cannot trust, not a
                # crash. rules/evaluate.py treats the same hazard as
                # UNKNOWN via a try/except; the gate blocks the same way
                # rather than raising past its caller.
                return Verdict.blocked(
                    GateCheck.BOOKING_CITES_RULE,
                    BlockKind.GROUNDING,
                    f"{rule.ref()} requires {needed} hours lead time and "
                    f"hours_until_appointment ({available!r}) cannot be compared to it",
                )
            if unmet:
                return Verdict.blocked(
                    GateCheck.BOOKING_CITES_RULE,
                    BlockKind.GROUNDING,
                    f"{rule.ref()} requires {needed} hours lead time, only "
                    f"{available} available",
                )

        for rule in _unknown_of_type(ruleset, facts, "require_lead_time"):
            if rule.outcome.hours is None:
                continue
            gaps = ", ".join(missing_facts(rule, facts))
            return Verdict.blocked(
                GateCheck.BOOKING_CITES_RULE,
                BlockKind.GROUNDING,
                f"{rule.ref()} might require a longer lead time and we never "
                f"established {gaps}",
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
