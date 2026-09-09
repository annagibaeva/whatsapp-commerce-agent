"""The audit log's one job is to prove every booking was authorised.

`ToolContext._counter` starts at 0 on every turn (a fresh `ToolContext`
is built per inbound message -- see `wca.cli.run_job`), so the old
`proposal_id(n)` -- just `f"prop_{n:04d}"` -- handed out `prop_0001` to
the first proposal of *every* turn, on *every* thread. `AuditLog` is
process-wide (built once in `build_serve_app`) and dedups `append` on
that exact id, so the second booking a process ever sees -- on any
thread -- silently failed to append its audit record.

These tests pin the actual failure (two threads, two turns) and the
properties the fix must keep (dedup still works, no thread-id leakage,
ids still order proposals within a turn).
"""

from __future__ import annotations

from datetime import timedelta

from wca.audit import AuditLog
from wca.calendar.mock import MockCalendar, Slot
from wca.catalogue import load_catalogue
from wca.clock import utc
from wca.conversation.state import ConversationState
from wca.escalation import Template, TemplateRegistry
from wca.ids import proposal_id
from wca.models import Action, AuditRecord, Proposal, Verdict
from wca.rules.store import load_ruleset
from wca.tools import ToolContext, request_booking

RULES = load_ruleset("policy/salon.rules.json")
CATALOGUE = load_catalogue("policy/salon.catalogue.json")
REGISTRY = TemplateRegistry(templates=(
    Template(reason="general", name="general_notice", approved=True),
))

NOW = utc(2026, 8, 21, 10)  # a Friday
STARTS_AT = NOW + timedelta(hours=72)  # a Monday -- clear of every lead-time rule
SLOT_A = "s_a"
SLOT_B = "s_b"

#: A returning adult customer -- no ask, no escalation, no deny. The
#: cleanest possible path to a "book" proposal, so nothing but the id
#: bug can make these tests fail.
FACTS = {
    "is_first_colour_visit": False,
    "customer_is_over_16": True,
    "requested_weekday": "monday",
}


def _ctx(audit: AuditLog, thread_id: str, calendar: MockCalendar, now=NOW) -> ToolContext:
    conversation = ConversationState(thread_id=thread_id, last_inbound_at=now, facts=dict(FACTS))
    return ToolContext(
        ruleset=RULES, catalogue=CATALOGUE, calendar=calendar,
        registry=REGISTRY, audit=audit, conversation=conversation, now=now,
    )


# --- the bug, pinned -------------------------------------------------------

def test_two_threads_each_booking_produce_two_audit_records():
    """The exact scenario the PRD's audit-record promise depends on: a
    process-wide AuditLog, two different customers, both booking. Today
    (before the fix) thread B's proposal_id collides with thread A's
    (both "prop_0001" -- each ToolContext's counter starts fresh), so
    AuditLog.append silently drops thread B's record."""
    audit = AuditLog()
    calendar = MockCalendar(slots=[Slot(SLOT_A, STARTS_AT), Slot(SLOT_B, STARTS_AT)])

    ctx_a = _ctx(audit, "447700900111", calendar)
    result_a = request_booking(ctx_a, service_id="svc_colour_full", slot_id=SLOT_A)
    assert result_a["ok"] is True

    ctx_b = _ctx(audit, "447700900222", calendar)
    result_b = request_booking(ctx_b, service_id="svc_colour_full", slot_id=SLOT_B)
    assert result_b["ok"] is True

    assert len(audit.records()) == 2


def test_the_same_thread_on_two_separate_turns_produces_two_audit_records():
    """A fresh ToolContext is built per turn even for one repeat customer.
    Same collision, same silent drop, if the id is only ever
    counter-scoped within a turn."""
    audit = AuditLog()
    calendar = MockCalendar(slots=[Slot(SLOT_A, STARTS_AT), Slot(SLOT_B, STARTS_AT)])

    ctx_turn1 = _ctx(audit, "447700900111", calendar, now=NOW)
    result1 = request_booking(ctx_turn1, service_id="svc_colour_full", slot_id=SLOT_A)
    assert result1["ok"] is True

    ctx_turn2 = _ctx(audit, "447700900111", calendar, now=NOW + timedelta(hours=1))
    result2 = request_booking(ctx_turn2, service_id="svc_colour_full", slot_id=SLOT_B)
    assert result2["ok"] is True

    assert len(audit.records()) == 2


# --- what the fix must not break --------------------------------------------

def test_appending_the_genuinely_same_proposal_twice_still_produces_one_record():
    """The dedup itself is correct and must survive the fix -- it is what
    protects against a redelivered webhook appending the same decision
    twice."""
    audit = AuditLog()
    pid = proposal_id("447700900111", NOW, 1)
    proposal = Proposal(
        proposal_id=pid, thread_id="447700900111",
        action=Action(type="book", slot_id="s1"),
        cited_rules=(), facts={}, created_at=NOW,
    )
    record = AuditRecord(
        proposal=proposal, verdict=Verdict.passed(), ruleset_version="v1",
        rules_english=(), calendar_read={}, window_read={}, decided_at=NOW,
    )

    assert audit.append(record) is True
    assert audit.append(record) is False
    assert len(audit.records()) == 1


def test_a_proposal_id_contains_no_run_of_the_thread_ids_digits():
    """Thread ids are customer phone numbers. None of them may leak into
    an identifier that ends up in an audit record or a log line."""
    thread_id = "447700900123"
    pid = proposal_id(thread_id, NOW, 1)

    assert thread_id not in pid
    for length in range(4, len(thread_id) + 1):
        for start in range(0, len(thread_id) - length + 1):
            run = thread_id[start:start + length]
            assert run not in pid, f"digit run {run!r} leaked into {pid!r}"


def test_two_proposals_within_one_turn_get_different_ids_that_sort_in_order():
    a = proposal_id("447700900111", NOW, 1)
    b = proposal_id("447700900111", NOW, 2)

    assert a != b
    assert sorted([b, a]) == [a, b]
