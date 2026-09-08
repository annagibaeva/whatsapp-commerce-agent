from datetime import timedelta

from wca.audit import AuditLog
from wca.calendar.mock import MockCalendar, Slot
from wca.catalogue import load_catalogue
from wca.clock import utc
from wca.conversation.state import ConversationState
from wca.escalation import Template, TemplateRegistry
from wca.models import BlockKind
from wca.rules.store import load_ruleset
from wca.tools import (
    ToolContext,
    check_availability,
    escalate,
    request_booking,
    search_catalogue,
)

RULES = load_ruleset("policy/salon.rules.json")
CATALOGUE = load_catalogue("policy/salon.catalogue.json")

NOW = utc(2026, 8, 21, 10)
FIRST_COLOUR_SLOT = "s_first_colour"
FIRST_COLOUR_STARTS_AT = NOW + timedelta(hours=10)
LATER_SLOT = "s_later"
LATER_STARTS_AT = NOW + timedelta(hours=72)

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
    calendar = _calendar()
    facts = dict(ADULT_RETURNING_FACTS, requested_weekday="sunday")
    ctx = _ctx(calendar, facts=facts)

    result = request_booking(ctx, service_id="svc_colour_full", slot_id=LATER_SLOT)

    assert result["ok"] is False
    assert "Sunday" in result["reason"] or "sunday" in result["reason"].lower()
    assert calendar.bookings() == ()


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
