"""The tool loop, driven by a scripted stub client. No network."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest

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


def _ctx(calendar: MockCalendar, facts: dict | None = None, now=NOW) -> ToolContext:
    conversation = ConversationState(thread_id="t1", last_inbound_at=now, facts=dict(facts or {}))
    return ToolContext(
        ruleset=RULES, catalogue=CATALOGUE, calendar=calendar,
        registry=REGISTRY, audit=AuditLog(), conversation=conversation, now=now,
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
class FakeUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class FakeResponse:
    stop_reason: str
    content: list[FakeBlock]
    #: Absent (`None`) on every response built by `_tool_call`/`_final_text`
    #: below -- this is what proves `Agent` tolerates a stub that reports
    #: no usage at all (only opt-in stubs, and the real SDK, carry one).
    #: The cost tests further down build `FakeResponse` directly with a
    #: real `FakeUsage` instead.
    usage: Any = None


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
        "is_first_colour_visit": True, "customer_is_over_16": True, "requested_weekday": "tuesday",
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
        "is_first_colour_visit": False, "customer_is_over_16": True, "requested_weekday": "tuesday",
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
        "is_first_colour_visit": False, "customer_is_over_16": True, "requested_weekday": "tuesday",
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


# --- the system prompt carries the current date, from `now`, not the wall --

def test_the_system_prompt_carries_ctxs_now_including_the_weekday():
    """A real customer asked for an appointment and the agent asked what
    today's date is -- the model was never told. NOW here is a Friday;
    the rendered prompt must say so, not leave the model to guess or
    compute it, and it must come from ctx.now, not datetime.now()."""
    calendar = _calendar()
    ctx = _ctx(calendar)
    client = FakeClient(script=[_final_text("hello")])

    agent = Agent(client=client, tool_context=ctx)

    assert "Friday" in agent.system
    assert "21 August 2026" in agent.system


def test_the_system_prompt_changes_with_ctxs_now():
    """Proves the rendered date actually comes from ctx.now (not a
    hardcoded string, and not the wall clock): a different `now` on the
    context produces a different system prompt."""
    calendar = _calendar()
    ctx = _ctx(calendar, now=NOW + timedelta(days=2))  # Sunday
    client = FakeClient(script=[_final_text("hello")])

    agent = Agent(client=client, tool_context=ctx)

    assert "Sunday" in agent.system
    assert "23 August 2026" in agent.system


def test_the_rendered_prompt_leaves_no_placeholder_braces_behind():
    """`{now}` must actually be substituted -- a leftover `{` or `}` in the
    rendered prompt would mean either an unfilled placeholder or a stray
    literal brace in the template that `.format()` would choke on or
    silently mishandle. Since the template's only placeholder is `{now}`,
    a fully rendered prompt should carry no braces at all."""
    calendar = _calendar()
    ctx = _ctx(calendar)
    client = FakeClient(script=[_final_text("hello")])

    agent = Agent(client=client, tool_context=ctx)

    assert "{" not in agent.system
    assert "}" not in agent.system


# --- the loop supports acting and confirming in a single turn ---------------

def test_a_scripted_availability_then_booking_conversation_ends_in_one_reply():
    """Wiring test, not a behaviour test: scripts the model checking
    availability with no dates (the fixed plumbing bug -- it no longer
    needs one), requesting a booking, and then returning text, and
    asserts the loop runs that through to a single final reply rather
    than stopping partway and leaving the customer with a question. This
    proves the loop *supports* acting in one turn; it says nothing about
    whether the model actually will -- that part rests on the prompt."""
    calendar = _calendar()
    ctx = _ctx(calendar, facts={
        "is_first_colour_visit": False, "customer_is_over_16": True, "requested_weekday": "tuesday",
    })
    client = FakeClient(script=[
        _tool_call("check_availability", {"service_id": "svc_colour_full"}, call_id="call_1"),
        _tool_call(
            "request_booking",
            {"service_id": "svc_colour_full", "slot_id": LATER_SLOT},
            call_id="call_2",
        ),
        _final_text("Of course -- I've got you booked for Thursday at 10am."),
    ])
    agent = Agent(client=client, tool_context=ctx)

    reply = agent.run_turn([{"role": "user", "content": "can I get a full colour Thursday morning"}])

    assert len(calendar.bookings()) == 1
    assert reply == "Of course -- I've got you booked for Thursday at 10am."
    # Two tool rounds plus the final text-only response: no intermediate
    # question broke the loop, and only one reply was ever produced.
    assert len(client.messages.calls) == 3


# --- tool_calls: the structural record wca.cli reads to shape a reply -------

def test_run_turn_records_each_dispatched_tool_call_and_its_real_result():
    """`Agent.tool_calls` is what `wca.cli` reads to decide whether a
    reply can go out as an interactive message -- it must be the tools'
    actual return values, in call order, not something reconstructed
    from the model's text."""
    calendar = _calendar()
    ctx = _ctx(calendar, facts={
        "is_first_colour_visit": False, "customer_is_over_16": True, "requested_weekday": "tuesday",
    })
    client = FakeClient(script=[
        _tool_call("check_availability", {"service_id": "svc_colour_full"}, call_id="call_1"),
        _tool_call(
            "request_booking",
            {"service_id": "svc_colour_full", "slot_id": LATER_SLOT},
            call_id="call_2",
        ),
        _final_text("You're booked in."),
    ])
    agent = Agent(client=client, tool_context=ctx)

    agent.run_turn([{"role": "user", "content": "book me a colour"}])

    assert [name for name, _ in agent.tool_calls] == ["check_availability", "request_booking"]
    availability_result, booking_result = (result for _, result in agent.tool_calls)
    assert isinstance(availability_result, list)
    assert booking_result["ok"] is True


def test_a_fresh_agent_starts_with_no_tool_calls_recorded():
    calendar = _calendar()
    ctx = _ctx(calendar)
    client = FakeClient(script=[])
    agent = Agent(client=client, tool_context=ctx)

    assert agent.tool_calls == []


def test_tool_calls_resets_between_turns_on_the_same_agent():
    """A second `run_turn` must not carry over the first turn's record --
    otherwise wca.cli could shape a reply around a tool call from a
    conversation turn the customer never saw a reply to."""
    calendar = _calendar()
    ctx = _ctx(calendar)
    client = FakeClient(script=[
        _tool_call("search_catalogue", {"query": "colour"}),
        _final_text("first reply"),
        _final_text("second reply"),
    ])
    agent = Agent(client=client, tool_context=ctx)

    agent.run_turn([{"role": "user", "content": "first"}])
    assert len(agent.tool_calls) == 1

    agent.run_turn([{"role": "user", "content": "second"}])
    assert agent.tool_calls == []


# --- cost_usd / call_count: instrumenting the agent's own model calls ------

def test_run_turn_prices_every_model_call_it_makes():
    """Fix 3a: the second (and any later) model call per turn was
    previously invisible to any cost accounting. Scripts a tool call
    followed by a final text response -- two real model calls -- each
    carrying its own usage, and checks both `cost_usd` and `call_count`
    add up to the whole turn, not just the first call."""
    calendar = _calendar()
    ctx = _ctx(calendar)
    client = FakeClient(script=[
        FakeResponse(
            stop_reason="tool_use",
            content=[FakeBlock(type="tool_use", name="search_catalogue",
                                input={"query": "colour"}, id="call_1")],
            usage=FakeUsage(input_tokens=1000, output_tokens=100),
        ),
        FakeResponse(
            stop_reason="end_turn",
            content=[FakeBlock(type="text", text="here you go")],
            usage=FakeUsage(input_tokens=1200, output_tokens=50),
        ),
    ])
    agent = Agent(client=client, tool_context=ctx)

    reply = agent.run_turn([{"role": "user", "content": "what colours do you offer"}])

    assert reply == "here you go"
    assert agent.call_count == 2
    from wca.pricing import estimate_cost
    expected = (
        estimate_cost(MODEL, 1000, 100) + estimate_cost(MODEL, 1200, 50)
    )
    assert agent.cost_usd == pytest.approx(expected)
    # Sentinel: pricing only the first call's usage must NOT match --
    # otherwise this test would pass even if the second call's cost were
    # silently dropped.
    assert agent.cost_usd != pytest.approx(estimate_cost(MODEL, 1000, 100))


def test_cost_usd_and_call_count_reset_between_turns():
    from wca.pricing import estimate_cost

    calendar = _calendar()
    ctx = _ctx(calendar)
    client = FakeClient(script=[
        FakeResponse(
            stop_reason="end_turn",
            content=[FakeBlock(type="text", text="first")],
            usage=FakeUsage(input_tokens=100, output_tokens=10),
        ),
        FakeResponse(
            stop_reason="end_turn",
            content=[FakeBlock(type="text", text="second")],
            usage=FakeUsage(input_tokens=200, output_tokens=20),
        ),
    ])
    agent = Agent(client=client, tool_context=ctx)

    agent.run_turn([{"role": "user", "content": "first"}])
    first_cost = agent.cost_usd
    assert agent.call_count == 1
    assert first_cost == pytest.approx(estimate_cost(MODEL, 100, 10))

    agent.run_turn([{"role": "user", "content": "second"}])
    assert agent.call_count == 1
    # Just the second turn's own cost -- not first_cost added on top. A
    # fresh turn starts its accounting at zero.
    assert agent.cost_usd == pytest.approx(estimate_cost(MODEL, 200, 20))
    assert agent.cost_usd != pytest.approx(first_cost)


def test_a_stub_response_with_no_usage_at_all_still_counts_the_call():
    """Most of this file's stubs (`_tool_call`/`_final_text`) carry no
    `usage`. `Agent` must not crash on that -- it just cannot price a
    call it was told nothing about."""
    calendar = _calendar()
    ctx = _ctx(calendar)
    client = FakeClient(script=[_final_text("hello")])
    agent = Agent(client=client, tool_context=ctx)

    agent.run_turn([{"role": "user", "content": "hi"}])

    assert agent.call_count == 1
    assert agent.cost_usd == 0.0
