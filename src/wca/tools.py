"""The four tools the model gets.

The model can search the catalogue, check availability, ask for a
booking, and escalate to a human. It cannot do anything else. In
particular it cannot commit a slot on its own: `request_booking` builds a
Proposal from the conversation's own facts, holds the slot, and only
commits if `wca.gate.evaluate` returns a passing verdict. Everything
about that decision -- which facts, which rules, which verdict -- is
computed in this module or below it. Nothing here trusts the model for a
number a rule depends on.

Every tool takes a `ToolContext` explicitly. No module-level state, no
singleton calendar, no clock read from the wall. A caller that wants two
independent conversations running at once just builds two contexts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from wca.audit import AuditLog
from wca.calendar.mock import HoldRefused, MockCalendar
from wca.catalogue import Catalogue, facts_for
from wca.conversation.state import ConversationState
from wca.escalation import TemplateRegistry, window_view
from wca.gate import evaluate
from wca.ids import idempotency_key as make_idempotency_key
from wca.ids import proposal_id as make_proposal_id
from wca.models import Action, AuditRecord, Proposal
from wca.propose import propose
from wca.rules.render import to_english
from wca.rules.schema import RuleSet


@dataclass
class ToolContext:
    """Everything one tool call needs, gathered in one place.

    `now` is passed in rather than read from the clock, same rule as the
    rest of this codebase: library code never calls `datetime.now()`.
    `_counter` backs the proposal ids handed to `propose()`; it lives
    here instead of as a free-standing global so two contexts never
    collide on the same counter.
    """

    ruleset: RuleSet
    catalogue: Catalogue
    calendar: MockCalendar
    registry: TemplateRegistry
    audit: AuditLog
    conversation: ConversationState
    now: datetime
    _counter: int = field(default=0, repr=False)

    def next_counter(self) -> int:
        self._counter += 1
        return self._counter


def search_catalogue(ctx: ToolContext, query: str) -> list[dict[str, Any]]:
    """Read-only. Touches nothing but the catalogue."""
    return [
        {
            "id": service.id,
            "name": service.name,
            "category": service.category,
            "duration_minutes": service.duration_minutes,
            "price_minor": service.price_minor,
        }
        for service in ctx.catalogue.search(query)
    ]


def check_availability(
    ctx: ToolContext, service_id: str, from_date: str, to_date: str
) -> list[dict[str, Any]]:
    """Read-only. Free means `calendar.availability(now)` includes the slot.

    An unknown service returns no slots rather than guessing at what the
    customer meant. Dates are plain `YYYY-MM-DD` strings, inclusive.
    """
    if ctx.catalogue.get(service_id) is None:
        return []

    start = date.fromisoformat(from_date)
    end = date.fromisoformat(to_date)

    free_ids = ctx.calendar.availability(ctx.now)
    results: list[dict[str, Any]] = []
    for slot_id in free_ids:
        slot = ctx.calendar.slot(slot_id)
        if slot is None or slot.starts_at is None:
            continue
        if start <= slot.starts_at.date() <= end:
            results.append({"slot_id": slot.slot_id, "starts_at": slot.starts_at.isoformat()})

    results.sort(key=lambda r: r["starts_at"])
    return results


def request_booking(ctx: ToolContext, service_id: str, slot_id: str) -> dict[str, Any]:
    """The gated tool. See the module docstring for the invariant this keeps.

    Order: look up the service; build facts (conversation facts, then
    catalogue facts, then the *computed* hours-until-appointment, in that
    order, so the computed number always wins over anything the model or
    an earlier turn put in the conversation's facts); propose; hold;
    evaluate; commit on PASS or release on BLOCK; audit either way.
    """
    service = ctx.catalogue.get(service_id)
    if service is None:
        return {"ok": False, "reason": f"no such service: {service_id}"}

    facts: dict[str, Any] = dict(ctx.conversation.facts)
    facts.update(facts_for(service))
    hours = ctx.calendar.hours_until(slot_id, ctx.now)
    if hours is None:
        # An unknown slot has no start time to compute from. Drop
        # whatever the conversation claimed rather than trust it -- a
        # missing fact makes any lead-time rule block on "never
        # established", which is the safe direction to be wrong in.
        facts.pop("hours_until_appointment", None)
    else:
        facts["hours_until_appointment"] = hours

    proposal = propose(
        thread_id=ctx.conversation.thread_id,
        facts=facts,
        ruleset=ctx.ruleset,
        slot_id=slot_id,
        now=ctx.now,
        counter=ctx.next_counter(),
    )
    cited_rules = [
        rule
        for rule in (ctx.ruleset.get(ref.rule_id, ref.version) for ref in proposal.cited_rules)
        if rule is not None
    ]

    try:
        hold = ctx.calendar.hold(slot_id, thread_id=ctx.conversation.thread_id, now=ctx.now)
    except HoldRefused as refused:
        # No gate call. There is nothing to hold, so there is nothing
        # for the gate to check.
        return {"ok": False, "reason": f"{refused.reason.value}: {slot_id}"}

    proposal = proposal.model_copy(update={"hold_id": hold.hold_id})
    calendar_view = ctx.calendar.view(slot_id, ctx.now)
    window_read = window_view(
        ctx.conversation.last_inbound_at,
        ctx.now,
        proposal.action.escalation_reason or "",
        ctx.registry,
    )
    verdict = evaluate(proposal, ctx.ruleset, calendar_view, window_read)

    # A PASS on a non-"book" proposal (e.g. propose() decided the honest
    # answer is "ask" or "decline") is not a licence to commit. The gate
    # only exercises its booking checks when proposal.action.type ==
    # "book"; anything else evaluates those checks vacuously true. This
    # extra check is what keeps that from becoming a second way in.
    if verdict.allowed and proposal.action.type == "book":
        key = make_idempotency_key(
            ctx.conversation.thread_id, "book", {"service_id": service_id, "slot_id": slot_id}
        )
        booking = ctx.calendar.commit(hold.hold_id, idempotency_key=key, now=ctx.now)
        result: dict[str, Any] = {
            "ok": True,
            "booking_id": booking.booking_id,
            "slot_id": booking.slot_id,
        }
    else:
        ctx.calendar.release(hold.hold_id, reason=verdict.reason or proposal.action.type, now=ctx.now)
        if not verdict.allowed:
            # A real gate block: the reason the gate gave, verbatim.
            reason = verdict.reason
        elif proposal.action.type == "decline":
            # propose() itself declined (a deny rule matched) before the
            # gate ever saw a "book" proposal to check. Surface the
            # matched rule's own reason rather than a bare action name.
            denying = [r for r in cited_rules if r.outcome.type == "deny"]
            reason = denying[0].outcome.reason if denying else "this cannot be booked"
        elif proposal.action.type == "escalate":
            reason = f"this needs a person to review: {proposal.action.escalation_reason}"
        else:  # "ask": propose() found a fact it still needs
            reason = proposal.action.question or "I need more information before I can book this"
        result = {"ok": False, "reason": reason}

    ctx.audit.append(AuditRecord(
        proposal=proposal,
        verdict=verdict,
        ruleset_version=ctx.ruleset.ruleset_version,
        rules_english=tuple(to_english(rule.condition) for rule in cited_rules),
        calendar_read=calendar_view,
        window_read=window_read,
        decided_at=ctx.now,
    ))
    return result


def escalate(ctx: ToolContext, reason: str) -> dict[str, Any]:
    """Ask for a human. Gated on whether one can still be reached.

    No slot is involved, so there is nothing to hold and nothing to cite
    -- an escalation proposal carries no cited rules, which is also what
    keeps the gate's "more specific rule was left out" check (check 3)
    from ever firing on it: that check only compares against what was
    cited, and an empty citation list makes every such comparison
    vacuous.
    """
    proposal = Proposal(
        proposal_id=make_proposal_id(ctx.next_counter()),
        thread_id=ctx.conversation.thread_id,
        action=Action(type="escalate", escalation_reason=reason),
        cited_rules=(),
        facts=dict(ctx.conversation.facts),
        created_at=ctx.now,
    )
    window_read = window_view(ctx.conversation.last_inbound_at, ctx.now, reason, ctx.registry)
    verdict = evaluate(proposal, ctx.ruleset, {}, window_read)

    if verdict.allowed:
        result: dict[str, Any] = {
            "ok": True,
            "message": "a human has been notified",
            "template": window_read.get("template_name"),
        }
    else:
        result = {"ok": False, "reason": verdict.reason}

    ctx.audit.append(AuditRecord(
        proposal=proposal,
        verdict=verdict,
        ruleset_version=ctx.ruleset.ruleset_version,
        rules_english=(),
        calendar_read={},
        window_read=window_read,
        decided_at=ctx.now,
    ))
    return result


#: Tool definitions in the shape the Anthropic Messages API wants. Passed
#: to `client.messages.create(tools=...)` verbatim.
TOOL_SPECS: tuple[dict[str, Any], ...] = (
    {
        "name": "search_catalogue",
        "description": "Search the salon's service catalogue by name or category. Read-only.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "check_availability",
        "description": (
            "List free appointment slots for a service between two dates "
            "(YYYY-MM-DD, inclusive). Read-only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service_id": {"type": "string"},
                "from_date": {"type": "string", "description": "YYYY-MM-DD"},
                "to_date": {"type": "string", "description": "YYYY-MM-DD"},
            },
            "required": ["service_id", "from_date", "to_date"],
        },
    },
    {
        "name": "request_booking",
        "description": (
            "Ask to book a service at a slot. This does not guarantee a booking "
            "-- a policy check can refuse it. A refusal comes back with a reason "
            "meant to be explained to the customer, not retried around."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service_id": {"type": "string"},
                "slot_id": {"type": "string"},
            },
            "required": ["service_id", "slot_id"],
        },
    },
    {
        "name": "escalate",
        "description": (
            "Raise the conversation to a human, with a short reason. Can be "
            "refused if a human cannot be reached before the messaging window closes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
)

_HANDLERS = {
    "search_catalogue": lambda ctx, args: search_catalogue(ctx, **args),
    "check_availability": lambda ctx, args: check_availability(ctx, **args),
    "request_booking": lambda ctx, args: request_booking(ctx, **args),
    "escalate": lambda ctx, args: escalate(ctx, **args),
}


def dispatch(ctx: ToolContext, name: str, arguments: dict[str, Any]) -> Any:
    """Run one tool call by name. Used by the agent loop in `wca.agent`."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"ok": False, "reason": f"no such tool: {name}"}
    return handler(ctx, arguments)
