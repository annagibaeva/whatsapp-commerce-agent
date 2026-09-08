from datetime import timedelta

import pytest

from wca.audit import AuditLog
from wca.calendar.mock import HoldRefused, MockCalendar, RefusalReason, Slot
from wca.catalogue import load_catalogue
from wca.clock import utc
from wca.conversation.state import ConversationState
from wca.escalation import Template, TemplateRegistry
from wca.models import BlockKind
from wca.rules.store import load_ruleset
from wca.tools import (
    ToolContext,
    check_availability,
    dispatch,
    escalate,
    request_booking,
    search_catalogue,
)

RULES = load_ruleset("policy/salon.rules.json")
CATALOGUE = load_catalogue("policy/salon.catalogue.json")

NOW = utc(2026, 8, 21, 10)  # a Friday
FIRST_COLOUR_SLOT = "s_first_colour"
FIRST_COLOUR_STARTS_AT = NOW + timedelta(hours=10)
LATER_SLOT = "s_later"
LATER_STARTS_AT = NOW + timedelta(hours=72)  # a Monday
#: A slot that actually falls on a Sunday -- for proving
#: `no_colour_on_sunday` is checked against the slot's own start time,
#: not against whatever `requested_weekday` a conversation fact claims.
SUNDAY_SLOT = "s_sunday"
SUNDAY_STARTS_AT = NOW + timedelta(hours=48)  # a Sunday

REGISTRY = TemplateRegistry(templates=(
    Template(reason="general", name="general_notice", approved=True),
))

ADULT_RETURNING_FACTS = {
    "is_first_colour_visit": False,
    "customer_age": 30,
    "requested_weekday": "tuesday",
}

ADULT_FIRST_VISIT_FACTS = {
    "is_first_colour_visit": True,
    "customer_age": 30,
    "requested_weekday": "tuesday",
}


def _calendar() -> MockCalendar:
    return MockCalendar(slots=[
        Slot(FIRST_COLOUR_SLOT, FIRST_COLOUR_STARTS_AT),
        Slot(LATER_SLOT, LATER_STARTS_AT),
        Slot(SUNDAY_SLOT, SUNDAY_STARTS_AT),
    ])


def _ctx(calendar: MockCalendar, facts: dict | None = None, now=NOW, last_inbound_at=None) -> ToolContext:
    conversation = ConversationState(
        thread_id="t1",
        last_inbound_at=last_inbound_at or now,
        facts=dict(facts or {}),
    )
    return ToolContext(
        ruleset=RULES,
        catalogue=CATALOGUE,
        calendar=calendar,
        registry=REGISTRY,
        audit=AuditLog(),
        conversation=conversation,
        now=now,
    )


# --- the hostile-tool test -------------------------------------------------

def test_hostile_direct_call_for_a_first_colour_visit_too_soon_is_blocked():
    """A compromised model calls request_booking straight away.

    The conversation already carries is_first_colour_visit=True (as if
    extraction had already established it) and the slot is 10 hours out.
    The patch-test rule requires 48. No booking should exist afterwards.
    """
    calendar = _calendar()
    ctx = _ctx(calendar, facts=ADULT_FIRST_VISIT_FACTS)

    result = request_booking(ctx, service_id="svc_colour_full", slot_id=FIRST_COLOUR_SLOT)

    assert result["ok"] is False
    assert "48" in result["reason"]
    assert calendar.bookings() == ()

    records = ctx.audit.records()
    assert len(records) == 1
    assert records[0].verdict.allowed is False
    assert records[0].verdict.kind is BlockKind.GROUNDING


def test_hostile_test_is_not_vacuous_when_the_slot_is_far_enough_out():
    """Same shape, but the slot is 72 hours out. This one should pass.

    Proves the block above is the lead-time rule doing its job, not some
    unrelated failure that would block a first colour visit regardless.
    """
    calendar = _calendar()
    ctx = _ctx(calendar, facts=ADULT_FIRST_VISIT_FACTS)

    result = request_booking(ctx, service_id="svc_colour_full", slot_id=LATER_SLOT)

    assert result["ok"] is True
    assert len(calendar.bookings()) == 1


# --- hours_until_appointment cannot be injected -----------------------------

def test_hours_until_appointment_cannot_be_injected_by_the_model():
    """The conversation facts lie about lead time. The computed value wins."""
    calendar = _calendar()
    lying_facts = dict(ADULT_FIRST_VISIT_FACTS, hours_until_appointment=999)
    ctx = _ctx(calendar, facts=lying_facts)

    result = request_booking(ctx, service_id="svc_colour_full", slot_id=FIRST_COLOUR_SLOT)

    assert result["ok"] is False
    assert calendar.bookings() == ()

    record = ctx.audit.records()[0]
    # The computed number (10 hours), not the lie (999), reached the gate.
    assert record.proposal.facts["hours_until_appointment"] == 10.0


# --- read-only tools mutate nothing -----------------------------------------

def test_search_catalogue_mutates_nothing():
    calendar = _calendar()
    ctx = _ctx(calendar)
    holds_before = len(calendar._holds)
    bookings_before = len(calendar.bookings())
    audit_before = len(ctx.audit)

    results = search_catalogue(ctx, query="colour")

    assert len(results) == 2
    assert len(calendar._holds) == holds_before
    assert len(calendar.bookings()) == bookings_before
    assert len(ctx.audit) == audit_before


def test_check_availability_mutates_nothing():
    calendar = _calendar()
    ctx = _ctx(calendar)
    holds_before = len(calendar._holds)
    bookings_before = len(calendar.bookings())
    audit_before = len(ctx.audit)

    results = check_availability(
        ctx, service_id="svc_colour_full", from_date="2026-08-21", to_date="2026-08-25"
    )

    assert any(r["slot_id"] == FIRST_COLOUR_SLOT for r in results)
    assert len(calendar._holds) == holds_before
    assert len(calendar.bookings()) == bookings_before
    assert len(ctx.audit) == audit_before


def test_check_availability_on_an_unknown_service_returns_nothing():
    calendar = _calendar()
    ctx = _ctx(calendar)
    results = check_availability(
        ctx, service_id="svc_does_not_exist", from_date="2026-08-21", to_date="2026-12-31"
    )
    assert results == []


def test_check_availability_carries_a_human_readable_label():
    """The model should quote this back rather than computing a weekday
    or a 12-hour time itself -- that's a thing models get wrong."""
    calendar = _calendar()
    ctx = _ctx(calendar)

    results = check_availability(
        ctx, service_id="svc_colour_full", from_date="2026-08-21", to_date="2026-08-25"
    )

    match = next(r for r in results if r["slot_id"] == FIRST_COLOUR_SLOT)
    # FIRST_COLOUR_STARTS_AT is NOW (Friday 21 Aug, 10:00) + 10 hours = 20:00.
    assert match["label"] == "Friday 21 August, 8:00pm"


def test_check_availability_with_no_dates_defaults_to_a_window_from_now():
    """Fix 4: the model can ask "what's free" without doing date
    arithmetic on a `now` it is never told. Neither date given -> a
    sensible window from ctx.now, not an error."""
    calendar = _calendar()
    ctx = _ctx(calendar)

    results = check_availability(ctx, service_id="svc_colour_full")

    ids = {r["slot_id"] for r in results}
    # Both real slots fall within 14 days of NOW.
    assert ids == {FIRST_COLOUR_SLOT, LATER_SLOT, SUNDAY_SLOT}


def test_check_availability_with_only_from_date_defaults_the_end():
    calendar = _calendar()
    ctx = _ctx(calendar)

    results = check_availability(ctx, service_id="svc_colour_full", from_date="2026-08-21")

    ids = {r["slot_id"] for r in results}
    assert ids == {FIRST_COLOUR_SLOT, LATER_SLOT, SUNDAY_SLOT}


def test_check_availability_with_only_to_date_defaults_the_start():
    calendar = _calendar()
    ctx = _ctx(calendar)

    results = check_availability(ctx, service_id="svc_colour_full", to_date="2026-08-22")

    ids = {r["slot_id"] for r in results}
    assert ids == {FIRST_COLOUR_SLOT}


# --- a legitimate booking, and idempotency ----------------------------------

def test_a_legitimate_booking_succeeds_exactly_once():
    calendar = _calendar()
    ctx = _ctx(calendar, facts=ADULT_RETURNING_FACTS)

    result = request_booking(ctx, service_id="svc_colour_full", slot_id=LATER_SLOT)

    assert result["ok"] is True
    assert "booking_id" in result
    assert len(calendar.bookings()) == 1


def test_calling_request_booking_twice_still_produces_one_booking():
    calendar = _calendar()
    ctx = _ctx(calendar, facts=ADULT_RETURNING_FACTS)

    first = request_booking(ctx, service_id="svc_colour_full", slot_id=LATER_SLOT)
    second = request_booking(ctx, service_id="svc_colour_full", slot_id=LATER_SLOT)

    assert first["ok"] is True
    assert second["ok"] is False  # the slot is already booked, from itself
    assert len(calendar.bookings()) == 1


# --- a block carries the gate's reason text ---------------------------------

def test_a_denied_booking_returns_the_gates_reason_text():
    """The slot actually falls on a Sunday. The conversation never says
    so -- ADULT_RETURNING_FACTS claims "tuesday" -- but `requested_weekday`
    is derived from SUNDAY_STARTS_AT, not read off the conversation, so
    the Sunday rule fires anyway."""
    calendar = _calendar()
    ctx = _ctx(calendar, facts=ADULT_RETURNING_FACTS)

    result = request_booking(ctx, service_id="svc_colour_full", slot_id=SUNDAY_SLOT)

    assert result["ok"] is False
    assert "Sunday" in result["reason"] or "sunday" in result["reason"].lower()
    assert calendar.bookings() == ()


# --- requested_weekday cannot be injected, in either direction -------------

def test_requested_weekday_cannot_be_injected_by_the_model_to_book_a_sunday():
    """The bypass this task fixes: the model claims a weekday that is not
    the slot's real one, hoping the deny rule never sees "sunday". The
    slot's own starts_at must win, so this books nothing."""
    calendar = _calendar()
    lying_facts = dict(ADULT_RETURNING_FACTS, requested_weekday="tuesday")
    ctx = _ctx(calendar, facts=lying_facts)

    result = request_booking(ctx, service_id="svc_colour_full", slot_id=SUNDAY_SLOT)

    assert result["ok"] is False
    assert "sunday" in result["reason"].lower()
    assert calendar.bookings() == ()

    record = ctx.audit.records()[0]
    assert record.proposal.facts["requested_weekday"] == "sunday"


def test_requested_weekday_cannot_be_injected_by_the_model_to_block_a_tuesday():
    """The reverse direction: the model claims "sunday" for a slot that is
    really a Tuesday. A fix that only ever blocks is over-broad -- the
    derived weekday must win here too, and this books successfully."""
    calendar = _calendar()
    lying_facts = dict(ADULT_RETURNING_FACTS, requested_weekday="sunday")
    ctx = _ctx(calendar, facts=lying_facts)

    result = request_booking(ctx, service_id="svc_colour_full", slot_id=LATER_SLOT)

    assert result["ok"] is True
    assert len(calendar.bookings()) == 1

    record = ctx.audit.records()[0]
    assert record.proposal.facts["requested_weekday"] == "monday"


def test_an_unknown_service_is_refused_without_touching_the_calendar():
    calendar = _calendar()
    ctx = _ctx(calendar, facts=ADULT_RETURNING_FACTS)
    holds_before = len(calendar._holds)

    result = request_booking(ctx, service_id="svc_does_not_exist", slot_id=LATER_SLOT)

    assert result["ok"] is False
    assert len(calendar._holds) == holds_before
    assert len(ctx.audit) == 0


# --- escalate ----------------------------------------------------------------

def test_escalate_succeeds_when_a_human_can_still_be_reached():
    calendar = _calendar()
    ctx = _ctx(calendar)

    result = escalate(ctx, reason="general")

    assert result["ok"] is True
    assert len(ctx.audit) == 1


def test_escalate_is_blocked_when_the_window_is_about_to_close():
    calendar = _calendar()
    # last inbound 23 hours ago: 1 hour left, margin is 2.
    ctx = _ctx(calendar, last_inbound_at=NOW - timedelta(hours=23))

    result = escalate(ctx, reason="general")

    assert result["ok"] is False
    assert len(ctx.audit) == 1


def test_escalate_is_blocked_when_no_template_is_registered():
    calendar = _calendar()
    ctx = _ctx(calendar)

    result = escalate(ctx, reason="never_registered")

    assert result["ok"] is False
    assert "template" in result["reason"]


# --- I4: catalogue facts must win over conflicting conversation facts -------

def test_catalogue_facts_win_over_conflicting_conversation_facts():
    """request_booking merges conversation facts, then catalogue facts, so
    the catalogue wins on service_category and quoted_price_minor -- those
    two must come from the catalogue, not from anything the model (or an
    earlier turn) put in conversation.facts.

    Proven here by deliberately lying in the conversation facts
    (service_category="cut", a one-cent price) for a real colour booking.
    If the catalogue's values did not win, no rule in policy/salon.rules.json
    would match "cut" and the booking would be blocked with "no cited rule
    allows a booking" instead of succeeding.
    """
    calendar = _calendar()
    conflicting_facts = dict(
        ADULT_RETURNING_FACTS,
        service_category="cut",
        quoted_price_minor=1,
    )
    ctx = _ctx(calendar, facts=conflicting_facts)

    result = request_booking(ctx, service_id="svc_colour_full", slot_id=LATER_SLOT)

    assert result["ok"] is True
    record = ctx.audit.records()[0]
    assert record.proposal.facts["service_category"] == "colour"
    assert record.proposal.facts["quoted_price_minor"] == 18000


# --- I6: at most one successful booking per turn -----------------------------

def test_a_second_request_booking_on_the_same_context_is_refused():
    """One ToolContext is built fresh per agent turn (see wca.cli.run_job),
    so this proves at most one booking can succeed per turn even though
    the agent loop dispatches every tool_use block in a response."""
    calendar = _calendar()
    ctx = _ctx(calendar, facts=ADULT_RETURNING_FACTS)

    first = request_booking(ctx, service_id="svc_colour_full", slot_id=LATER_SLOT)
    second = request_booking(ctx, service_id="svc_colour_roots", slot_id=FIRST_COLOUR_SLOT)

    assert first["ok"] is True
    assert second["ok"] is False
    assert "already been made" in second["reason"]
    assert len(calendar.bookings()) == 1


# --- an AuditRecord exists even if commit() itself raises --------------------

def test_a_raise_from_commit_still_leaves_an_audit_record():
    """A verdict was already decided (PASS) by the time commit() runs. If
    commit() then raises -- a stale hold, an idempotency race -- the
    decision must still be on the audit trail, not silently lost."""
    calendar = _calendar()
    ctx = _ctx(calendar, facts=ADULT_RETURNING_FACTS)

    def _boom(*args, **kwargs):
        raise HoldRefused(RefusalReason.HOLD_EXPIRED, "boom")

    calendar.commit = _boom  # type: ignore[method-assign]

    with pytest.raises(HoldRefused):
        request_booking(ctx, service_id="svc_colour_full", slot_id=LATER_SLOT)

    assert len(ctx.audit) == 1
    assert ctx.audit.records()[0].verdict.allowed is True
    assert calendar.bookings() == ()


# --- C2: a malformed tool call is a refusal, not an exception ----------------

def test_check_availability_with_an_unparseable_date_is_a_refusal_not_a_crash():
    calendar = _calendar()
    ctx = _ctx(calendar)

    result = check_availability(
        ctx, service_id="svc_colour_full", from_date="next monday", to_date="2026-09-01"
    )

    assert result["ok"] is False
    assert "date" in result["reason"].lower()


def test_dispatch_refuses_rather_than_raises_on_a_bad_date_string():
    calendar = _calendar()
    ctx = _ctx(calendar)

    result = dispatch(ctx, "check_availability", {
        "service_id": "svc_colour_full", "from_date": "next monday", "to_date": "2026-09-01",
    })

    assert result["ok"] is False


def test_dispatch_refuses_rather_than_raises_on_a_missing_required_argument():
    calendar = _calendar()
    ctx = _ctx(calendar)

    result = dispatch(ctx, "search_catalogue", {})

    assert result["ok"] is False
    assert "search_catalogue" in result["reason"]


def test_dispatch_refuses_rather_than_raises_on_an_unexpected_keyword():
    calendar = _calendar()
    ctx = _ctx(calendar)

    result = dispatch(ctx, "request_booking", {
        "service_id": "svc_colour_full", "slot_id": LATER_SLOT, "not_a_real_argument": "x",
    })

    assert result["ok"] is False
    assert calendar.bookings() == ()


def test_dispatch_still_lets_a_real_programming_error_surface():
    """Only TypeError and ValueError are caught. Anything else (a bug, not
    a malformed argument) must still propagate -- swallowing it would hide
    the bug the rest of C2 is not meant to cover."""
    calendar = _calendar()
    ctx = _ctx(calendar)

    def _boom(ctx, args):
        raise KeyError("not an argument-shaped error")

    from wca import tools as tools_module
    original = tools_module._HANDLERS["search_catalogue"]
    tools_module._HANDLERS["search_catalogue"] = _boom
    try:
        with pytest.raises(KeyError):
            dispatch(ctx, "search_catalogue", {"query": "colour"})
    finally:
        tools_module._HANDLERS["search_catalogue"] = original


# --- I5: escalate's enum and the real registry cannot drift apart -----------

def test_escalate_tool_spec_carries_a_fixed_enum_of_reasons():
    from wca.tools import ESCALATION_REASONS, TOOL_SPECS

    escalate_spec = next(spec for spec in TOOL_SPECS if spec["name"] == "escalate")
    assert escalate_spec["input_schema"]["properties"]["reason"]["enum"] == list(ESCALATION_REASONS)
    assert "general" in ESCALATION_REASONS


# --- every fact the ruleset reads must be classified ------------------------

def test_every_fact_the_ruleset_reads_is_classified():
    """The point of this task: a new rule that reads a new booking fact
    must fail this test until someone classifies it, instead of silently
    falling through to being trusted from the model -- the exact shape of
    the `requested_weekday` bypass this task fixes.

    Walks every fact any live rule's condition actually mentions
    (`Rule.requires_facts`, derived at load time from the condition
    itself, so this cannot be faked by a rule with a stale
    `requires_facts` list) and demands each one is claimed by exactly one
    of `DERIVED_FACTS` or `CONVERSATIONAL_FACTS` in `wca.tools`.
    """
    from wca.tools import CONVERSATIONAL_FACTS, DERIVED_FACTS

    used: set[str] = set()
    for rule in RULES.rules:
        used |= set(rule.requires_facts)

    classified = DERIVED_FACTS | CONVERSATIONAL_FACTS
    unclassified = used - classified
    assert not unclassified, (
        f"the ruleset reads {sorted(unclassified)}, which is not in "
        "DERIVED_FACTS or CONVERSATIONAL_FACTS (wca.tools) -- classify it "
        "as describing the booking (derive it in code) or the customer "
        "(conversational) before this rule can be trusted"
    )
    assert not (DERIVED_FACTS & CONVERSATIONAL_FACTS), (
        "a fact cannot be both derived-from-the-booking and "
        "conversational -- pick one"
    )
