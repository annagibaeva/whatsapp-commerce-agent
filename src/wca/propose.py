"""Turning facts into a proposed action.

This is allowed to be wrong. It is the part a model influences, and the
gate exists to catch its mistakes. Keeping it simple keeps the gate the
interesting component.

Order matters. Deny beats everything. Then escalate. Then a missing fact
means ask. Only then do we propose a booking.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from wca.ids import proposal_id
from wca.models import Action, CitedRule, Proposal
from wca.rules.evaluate import missing_facts
from wca.rules.schema import RuleSet
from wca.rules.specificity import matching_rules, unknown_rules


def propose(
    thread_id: str,
    facts: dict[str, Any],
    ruleset: RuleSet,
    slot_id: str | None,
    now: datetime,
    counter: int,
) -> Proposal:
    matched = matching_rules(ruleset, facts)
    cited = tuple(CitedRule(rule_id=r.id, version=r.version) for r in matched)

    denying = [r for r in matched if r.outcome.type == "deny"]
    if denying:
        action = Action(type="decline")
    else:
        escalating = [r for r in matched if r.outcome.type == "require_escalation"]
        if escalating:
            action = Action(type="escalate", escalation_reason=escalating[0].id)
        else:
            gaps: list[str] = []
            for rule in unknown_rules(ruleset, facts):
                gaps.extend(missing_facts(rule, facts))
            if gaps:
                action = Action(
                    type="ask",
                    question=f"I still need to know: {', '.join(sorted(set(gaps)))}",
                )
            else:
                action = Action(type="book", slot_id=slot_id)

    return Proposal(
        proposal_id=proposal_id(counter),
        thread_id=thread_id,
        action=action,
        cited_rules=cited,
        facts=facts,
        created_at=now,
    )
