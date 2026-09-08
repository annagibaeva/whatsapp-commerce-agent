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
from datetime import date, datetime, timedelta
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
    #: One `ToolContext` is built fresh per agent turn (see `wca.cli`), so
    #: this flag is naturally turn-scoped. It stops a second successful
    #: `request_booking` call in the same turn -- the agent loop dispatches
    #: every tool_use block in a model response, and nothing else here
    #: limits how many of them can be `request_booking`.
    _booked_this_turn: bool = field(default=False, repr=False)

    def next_counter(self) -> int:
        self._counter += 1
        return self._counter


#: `datetime.weekday()` is Monday=0..Sunday=6. Indexing into this tuple
#: turns that into the same lowercase names the ruleset's `eq` comparisons
#: and `RawFactSet.requested_weekday` use.
WEEKDAY_NAMES: tuple[str, ...] = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)

#: How far ahead `check_availability` looks when the model asks "what's
#: free" without giving a date range. Long enough to be useful, short
#: enough that the result stays a short list, not the whole calendar.
DEFAULT_AVAILABILITY_WINDOW_DAYS = 14


def _weekday_of(starts_at: datetime) -> str:
    """The weekday name a rule can compare against, from a real slot time."""
    return WEEKDAY_NAMES[starts_at.weekday()]


def human_slot_label(starts_at: datetime) -> str:
    """"Tuesday 25 August, 2:00pm" -- for the model to quote back verbatim.

    Weekday names and 12-hour clock arithmetic are exactly the kind of
    thing a model gets wrong when asked to compute them from an ISO
    timestamp. Precomputing the label here means it never has to.

    Not private: `wca.transport.webhook`'s `/reminders/due` endpoint
    reuses this so n8n gets the same "Tuesday 25 August, 2:00pm" label
    `check_availability` already produces, rather than a second,
    independently-drifting copy of this formatting.
    """
    hour12 = starts_at.hour % 12 or 12
    period = "am" if starts_at.hour < 12 else "pm"
    return (
        f"{starts_at.strftime('%A')} {starts_at.day} {starts_at.strftime('%B')}, "
        f"{hour12}:{starts_at.minute:02d}{period}"
    )


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
    ctx: ToolContext, service_id: str, from_date: str | None = None, to_date: str | None = None
) -> list[dict[str, Any]] | dict[str, Any]:
    """Read-only. Free means `calendar.availability(now)` includes the slot.

    An unknown service returns no slots rather than guessing at what the
    customer meant. Dates are plain `YYYY-MM-DD` strings, inclusive.

    Both dates are optional. The model was asking "what's free" and
    getting stuck doing date arithmetic on `ctx.now` -- a value it is
    never told -- just to fill in a range it does not actually care
    about. Leaving either one out fills it in from `ctx.now`:
    `from_date` defaults to today, `to_date` to
    `DEFAULT_AVAILABILITY_WINDOW_DAYS` after whichever start is in play.

    A date the model could not format correctly (`"next monday"`, a
    malformed string) is a refusal with a reason the model can read and
    correct itself on, not an exception -- the caller cannot rely on
    `dispatch`'s own safety net alone, because that would return the same
    generic message for every malformed call instead of one that names
    which date was the problem.
    """
    if ctx.catalogue.get(service_id) is None:
        return []

    try:
        start = date.fromisoformat(from_date) if from_date else ctx.now.date()
        end = (
            date.fromisoformat(to_date)
            if to_date
            else start + timedelta(days=DEFAULT_AVAILABILITY_WINDOW_DAYS)
        )
    except (TypeError, ValueError) as exc:
        return {"ok": False, "reason": f"could not understand the date range given: {exc}"}

    free_ids = ctx.calendar.availability(ctx.now)
    results: list[dict[str, Any]] = []
    for slot_id in free_ids:
        slot = ctx.calendar.slot(slot_id)
        if slot is None or slot.starts_at is None:
            continue
        if start <= slot.starts_at.date() <= end:
            results.append({
                "slot_id": slot.slot_id,
                "starts_at": slot.starts_at.isoformat(),
                "label": human_slot_label(slot.starts_at),
            })

    results.sort(key=lambda r: r["starts_at"])
    return results


#: The principle: a fact that describes **the booking** must be derived
#: from the booking, in code, right here -- never trusted from the model
#: or an earlier conversation turn. A fact that describes **the
#: customer** can only ever come from the conversation; no code in this
#: repository could derive `is_first_colour_visit` or
#: `customer_is_over_16` from anything else. Every fact any rule in the
#: live ruleset actually reads
#: (`Rule.requires_facts`, across `RuleSet.rules`) must appear in exactly
#: one of these two sets -- see
#: `test_every_fact_the_ruleset_reads_is_classified` in test_tools.py.
#: That test is what turns "a new rule reads a new booking-describing
#: fact" from a silent hole (the model supplies it, same bug as
#: `requested_weekday` before this fix) into a failing test someone has
#: to look at.
#:
#: Adding a name to DERIVED_FACTS is a promise, not a wish -- it must
#: actually be computed somewhere below (or in `wca.catalogue.facts_for`)
#: before that promise is kept.
DERIVED_FACTS: frozenset[str] = frozenset({
    "service_category",  # wca.catalogue.facts_for -- from the catalogue
    "quoted_price_minor",  # wca.catalogue.facts_for -- from the catalogue
    "requested_weekday",  # below -- from the slot's own starts_at
    "hours_until_appointment",  # below -- from the slot's own starts_at
})

#: Facts only the customer's own words can establish.
CONVERSATIONAL_FACTS: frozenset[str] = frozenset({
    "is_first_colour_visit",
    "customer_is_over_16",
})


def request_booking(ctx: ToolContext, service_id: str, slot_id: str) -> dict[str, Any]:
    """The gated tool. See the module docstring for the invariant this keeps.

    Order: look up the service; build facts (conversation facts, then
    catalogue facts, then the *computed* hours-until-appointment and
    requested-weekday, in that order, so the computed values always win
    over anything the model or an earlier turn put in the conversation's
    facts); propose; hold; evaluate; commit on PASS or release on BLOCK;
    audit either way.
    """
    if ctx._booked_this_turn:
        # No proposal was built and nothing was held, so there is nothing
        # to audit -- same posture as the unknown-service refusal below,
        # which also returns before touching the audit log.
        return {"ok": False, "reason": "a booking has already been made in this conversation turn"}

    service = ctx.catalogue.get(service_id)
    if service is None:
        return {"ok": False, "reason": f"no such service: {service_id}"}

    facts: dict[str, Any] = dict(ctx.conversation.facts)
    facts.update(facts_for(service))

    slot = ctx.calendar.slot(slot_id)
    hours = ctx.calendar.hours_until(slot_id, ctx.now)
    if hours is None:
        # An unknown slot has no start time to compute from. Drop
        # whatever the conversation claimed rather than trust it -- a
        # missing fact makes any lead-time rule block on "never
        # established", which is the safe direction to be wrong in.
        facts.pop("hours_until_appointment", None)
    else:
        facts["hours_until_appointment"] = hours

    # Same move, same reason, for the weekday: `requested_weekday`
    # describes the slot being booked, not the customer, so it is the
    # slot's own `starts_at` that decides it -- never whatever the model
    # (or an earlier turn) put in conversation.facts. Popping first means
    # a slot with no known start time correctly leaves the fact missing
    # rather than keeping a stale or invented value around.
    facts.pop("requested_weekday", None)
    if slot is not None and slot.starts_at is not None:
        facts["requested_weekday"] = _weekday_of(slot.starts_at)

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
    #
    # The audit append is in `finally` rather than after this block: a
    # verdict was already decided by the time we reach here, so a record
    # of it must exist even if `commit()` itself goes on to raise (a hold
    # gone stale, an idempotency race). Without this, a PASS that failed
    # to actually commit would leave no trace of the decision at all.
    try:
        if verdict.allowed and proposal.action.type == "book":
            key = make_idempotency_key(
                ctx.conversation.thread_id, "book", {"service_id": service_id, "slot_id": slot_id}
            )
            booking = ctx.calendar.commit(
                hold.hold_id, idempotency_key=key, now=ctx.now, service_id=service_id
            )
            ctx._booked_this_turn = True
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
    finally:
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


#: The fixed set of reasons the model may hand `escalate`. Free text was
#: never checkable against `wca.cli`'s template registry -- the registry
#: is keyed by exact string, so a model saying "customer is angry" when
#: the registry only knows rule ids got "no template registered" every
#: time. Constraining the schema to this enum, and registering a template
#: for each one in `wca.cli.REGISTRY`, is what makes `escalate` usable in
#: production. "general" is the catch-all for anything that doesn't fit
#: the other reasons. Rule-triggered escalations (e.g.
#: "under_16_needs_guardian", produced by `wca.propose.propose`) are a
#: separate namespace -- they never go through this tool at all, they
#: come back out of `request_booking` -- so they keep working unchanged.
ESCALATION_REASONS: tuple[str, ...] = (
    "customer_requested_human",
    "customer_upset_or_angry",
    "outside_agent_scope",
    "technical_or_system_issue",
    "general",
)

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
            "(YYYY-MM-DD, inclusive). Read-only. Both dates are optional -- "
            "omit either or both to see what's free over the next "
            f"{DEFAULT_AVAILABILITY_WINDOW_DAYS} days from now. Each slot in "
            "the result carries a human-readable 'label' (e.g. 'Tuesday 25 "
            "August, 2:00pm') -- quote that back to the customer rather than "
            "computing a weekday or a 12-hour time yourself."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service_id": {"type": "string"},
                "from_date": {"type": "string", "description": "YYYY-MM-DD, optional"},
                "to_date": {"type": "string", "description": "YYYY-MM-DD, optional"},
            },
            "required": ["service_id"],
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
            "Raise the conversation to a human, with a reason chosen from the "
            "fixed list. Can be refused if a human cannot be reached before the "
            "messaging window closes. Use 'general' if nothing else fits."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "enum": list(ESCALATION_REASONS)},
            },
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
    """Run one tool call by name. Used by the agent loop in `wca.agent`.

    A malformed call -- a date string the model made up
    (`check_availability(from_date="next monday")`), a required argument
    left out (`search_catalogue({})`), an argument name that does not
    exist -- raises `ValueError` or `TypeError` from the handler itself,
    from plain Python argument binding in the `**args` unpacking above.
    Left alone, that exception propagates out through `run_turn`,
    `run_job` and `drain_thread`, gets printed server-side by
    `after_enqueue`, and the customer receives nothing: on WhatsApp,
    silence is indistinguishable from a dead number. Catching exactly
    these two argument-shaped errors and turning them into a normal
    tool-result dict lets the model see what went wrong and correct
    itself (or explain the problem to the customer) instead of the turn
    dying silently. Nothing else is caught here -- a real programming
    error (`AttributeError`, `KeyError`, ...) still propagates, because
    swallowing it would hide the bug this function's own contract does
    not cover.
    """
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"ok": False, "reason": f"no such tool: {name}"}
    try:
        return handler(ctx, arguments)
    except (TypeError, ValueError) as exc:
        return {"ok": False, "reason": f"could not run {name} with those arguments: {exc}"}
