"""The tool loop, driven by a scripted stub client. No network."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import wca.agent as agent_module
from wca.agent import MAX_ITERATIONS, MODEL, Agent
from wca.audit import AuditLog
from wca.calendar.mock import MockCalendar, Slot
from wca.catalogue import load_catalogue
from wca.clock import utc
from wca.conversation.state import ConversationState
from wca.escalation import Template, TemplateRegistry
from wca.rules.store import load_ruleset
from wca.tools import ToolContext

RULES = load_ruleset("policy/salon.rules.json")
CATALOGUE = load_catalogue("policy/salon.catalogue.json")
REGISTRY = TemplateRegistry(templates=(
    Template(reason="general", name="general_notice", approved=True),
))

NOW = utc(2026, 8, 21, 10)
FIRST_COLOUR_SLOT = "s_first_colour"
FIRST_COLOUR_STARTS_AT = NOW + timedelta(hours=10)
LATER_SLOT = "s_later"
LATER_STARTS_AT = NOW + timedelta(hours=72)


def _calendar() -> MockCalendar:
    return MockCalendar(slots=[
        Slot(FIRST_COLOUR_SLOT, FIRST_COLOUR_STARTS_AT),
        Slot(LATER_SLOT, LATER_STARTS_AT),
    ])


def _ctx(calendar: MockCalendar, facts: dict | None = None) -> ToolContext:
    conversation = ConversationState(thread_id="t1", last_inbound_at=NOW, facts=dict(facts or {}))
    return ToolContext(
        ruleset=RULES, catalogue=CATALOGUE, calendar=calendar,
        registry=REGISTRY, audit=AuditLog(), conversation=conversation, now=NOW,
    )


# --- a scripted stub, shaped like the parts of the real SDK response we use -

@dataclass
class FakeBlock:
    type: str
    text: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None
    id: str | None = None


@dataclass
class FakeResponse:
    stop_reason: str
    content: list[FakeBlock]


class FakeMessages:
    def __init__(self, script: list[FakeResponse]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if not self._script:
            raise AssertionError("the stub ran out of scripted responses")
        return self._script.pop(0)


class FakeClient:
    def __init__(self, script: list[FakeResponse]) -> None:
        self.messages = FakeMessages(script)


def _tool_call(name: str, input: dict[str, Any], call_id: str = "call_1") -> FakeResponse:
    return FakeResponse(
        stop_reason="tool_use",
        content=[FakeBlock(type="tool_use", name=name, input=input, id=call_id)],
    )


def _final_text(text: str) -> FakeResponse:
    return FakeResponse(stop_reason="end_turn", content=[FakeBlock(type="text", text=text)])


# --- the hostile-tool test, run through the agent loop ----------------------

def test_hostile_tool_call_through_the_agent_produces_no_booking():
    """A compromised model skips straight to request_booking.

    Scripted here as: the very first model response is a tool_use block
    calling request_booking directly for a first colour visit 10 hours
    out, no search or availability check first. The gate must still stop
    it -- the agent loop gives the model tools, not trust.
    """
    calendar = _calendar()
    ctx = _ctx(calendar, facts={
        "is_first_colour_visit": True, "customer_age": 30, "requested_weekday": "tuesday",
    })
    client = FakeClient(script=[
        _tool_call("request_booking", {"service_id": "svc_colour_full", "slot_id": FIRST_COLOUR_SLOT}),
        _final_text("Sorry, that visit needs 48 hours' notice for a patch test."),
    ])
    agent = Agent(client=client, tool_context=ctx)

    reply = agent.run_turn([{"role": "user", "content": "book me in for colour tomorrow morning"}])

    assert calendar.bookings() == ()
    assert len(ctx.audit) == 1
    assert ctx.audit.records()[0].verdict.allowed is False
    assert "48" in reply


def test_a_legitimate_multi_tool_conversation_books_once():
    calendar = _calendar()
    ctx = _ctx(calendar, facts={
        "is_first_colour_visit": False, "customer_age": 30, "requested_weekday": "tuesday",
    })
    client = FakeClient(script=[
        _tool_call("search_catalogue", {"query": "colour"}, call_id="call_1"),
        _tool_call("request_booking", {"service_id": "svc_colour_full", "slot_id": LATER_SLOT}, call_id="call_2"),
        _final_text("You're booked in."),
    ])
    agent = Agent(client=client, tool_context=ctx)

    reply = agent.run_turn([{"role": "user", "content": "I'd like a full colour"}])

    assert len(calendar.bookings()) == 1
    assert reply == "You're booked in."
    # Three round trips: two tool calls plus the final text-only response.
    assert len(client.messages.calls) == 3


def test_two_request_booking_calls_in_one_response_produce_only_one_booking():
    """I6: the agent loop dispatches every tool_use block in a response.

    Scripted as a single model response carrying two request_booking
    tool_use blocks for two different slots, both of which would pass
    the gate on their own. Only the first may succeed; the second must
    come back refused, and the calendar must show exactly one booking.
    """
    calendar = _calendar()
    ctx = _ctx(calendar, facts={
        "is_first_colour_visit": False, "customer_age": 30, "requested_weekday": "tuesday",
    })
    client = FakeClient(script=[
        FakeResponse(stop_reason="tool_use", content=[
            FakeBlock(
                type="tool_use", name="request_booking",
                input={"service_id": "svc_colour_full", "slot_id": LATER_SLOT}, id="call_1",
            ),
            FakeBlock(
                type="tool_use", name="request_booking",
                input={"service_id": "svc_colour_full", "slot_id": FIRST_COLOUR_SLOT}, id="call_2",
            ),
        ]),
        _final_text("You're booked in."),
    ])
    agent = Agent(client=client, tool_context=ctx)

    agent.run_turn([{"role": "user", "content": "book me twice please"}])

    assert len(calendar.bookings()) == 1


def test_the_loop_stops_at_the_iteration_cap():
    """A model that never stops calling tools does not run forever."""
    calendar = _calendar()
    ctx = _ctx(calendar)
    script = [_tool_call("search_catalogue", {"query": "colour"}) for _ in range(MAX_ITERATIONS + 3)]
    client = FakeClient(script=script)
    agent = Agent(client=client, tool_context=ctx)

    reply = agent.run_turn([{"role": "user", "content": "hi"}])

    # MAX_ITERATIONS rounds, plus the initial call: MAX_ITERATIONS + 1 total.
    assert len(client.messages.calls) == MAX_ITERATIONS + 1
    assert reply == ""  # the last response was still a tool call, no text


# --- model and sampling parameters ------------------------------------------

def test_the_model_id_is_haiku():
    assert MODEL == "claude-haiku-4-5"


def test_agent_module_never_sends_sampling_parameters():
    """temperature/top_p/top_k were removed on the current model family.

    Grep the source for each as a keyword argument (`name=`), so a
    mention in a comment or docstring -- like the one right above this
    module's imports, warning not to send these -- does not itself trip
    the check. This stays true even if the request-building code moves
    around.
    """
    source = inspect.getsource(agent_module)
    for banned in ("temperature", "top_p", "top_k"):
        assert f"{banned}=" not in source, f"agent.py must never send {banned!r}"


def test_no_scripted_call_ever_carries_a_sampling_parameter():
    """Belt and braces: check the actual request kwargs a run produces."""
    calendar = _calendar()
    ctx = _ctx(calendar)
    client = FakeClient(script=[_final_text("hello")])
    agent = Agent(client=client, tool_context=ctx)

    agent.run_turn([{"role": "user", "content": "hi"}])

    for call in client.messages.calls:
        for banned in ("temperature", "top_p", "top_k"):
            assert banned not in call
