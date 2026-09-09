"""The webhook wired to the pipeline, end to end, with no network.

`build_serve_app` (in `wca.cli`) is the same composition `cmd_serve`
runs in production, minus the real Anthropic client and the real
WhatsApp transport. These tests hand it a stubbed model client and a
`FakeTransport` instead, and drive it through the real ASGI app -- the
same `create_app` that `test_webhook.py` exercises -- so what is proven
here is the wiring, not a reimplementation of it.

No test in this file sleeps and then hopes a race resolved the way it
was supposed to. Ordering is proven either by construction (a raw ASGI
call, recording the exact order two events were emitted in) or by
recording enter/exit events from concurrent threads and checking they
never interleave -- the same pattern `tests/test_conversation.py` uses
for `ThreadQueue` itself.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fastapi.testclient import TestClient

from wca.calendar.mock import MockCalendar, Slot
from wca.catalogue import load_catalogue
from wca.cli import build_serve_app
from wca.extract.base import ExtractionResult, RawFactSet
from wca.extract.fake import FakeExtractor
from wca.rules.store import load_ruleset
from wca.tools import human_slot_label
from wca.transport.fake import FakeTransport
from wca.transport.webhook import WebhookSettings

SECRET = "wiring-secret"
VERIFY_TOKEN = "wiring-verify-token"

RULES = load_ruleset("policy/salon.rules.json")
CATALOGUE = load_catalogue("policy/salon.catalogue.json")


# --- a stubbed model client: no network, no real SDK object -----------------

@dataclass
class _Block:
    type: str
    text: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None
    id: str | None = None


@dataclass
class _Response:
    stop_reason: str
    content: list[_Block]


class _StubMessages:
    """Always answers in one round trip. Never calls a tool.

    These wiring tests are about whether a message reaches the agent
    and a reply reaches the transport, not about the tool loop itself
    -- `tests/test_agent.py` already covers that in depth.
    """

    def __init__(self, on_create: Callable[[str], None] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._on_create = on_create

    def create(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        text = kwargs["messages"][-1]["content"]
        if self._on_create is not None:
            self._on_create(text)
        return _Response(stop_reason="end_turn", content=[_Block(type="text", text=f"ok: {text}")])


class _StubClient:
    def __init__(self, on_create: Callable[[str], None] | None = None) -> None:
        self.messages = _StubMessages(on_create=on_create)


class _ScriptedMessages:
    """Replays a fixed script of responses, tool calls included.

    Unlike `_StubMessages`, which always answers in one round trip, this
    is what the C1 end-to-end test and the I7 budget test need: a client
    that can call `request_booking` (or return no text at all) exactly
    the way `tests/test_agent.py`'s `FakeClient` does.
    """

    def __init__(self, script: list[_Response]) -> None:
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        if not self._script:
            raise AssertionError("the stub ran out of scripted responses")
        return self._script.pop(0)


class _ScriptedClient:
    def __init__(self, script: list[_Response]) -> None:
        self.messages = _ScriptedMessages(script)


class _RaisingMessages:
    """Simulates an unexpected failure partway through a turn."""

    def create(self, **kwargs: Any) -> _Response:
        raise RuntimeError("simulated model outage")


class _RaisingClient:
    def __init__(self) -> None:
        self.messages = _RaisingMessages()


def _tool_call(name: str, input: dict[str, Any], call_id: str = "call_1") -> _Response:
    return _Response(stop_reason="tool_use", content=[_Block(type="tool_use", name=name, input=input, id=call_id)])


def _final_text(text: str) -> _Response:
    return _Response(stop_reason="end_turn", content=[_Block(type="text", text=text)])


def _no_text_reply() -> _Response:
    """A response the model stopped on without ever producing text."""
    return _Response(stop_reason="end_turn", content=[])


# --- building signed webhook payloads, same shape as test_webhook.py --------

def _payload(message_id: str, from_: str, text: str, timestamp: str = "1755772800") -> dict[str, Any]:
    return {"object": "whatsapp_business_account", "entry": [{"changes": [
        {"field": "messages", "value": {"messages": [{
            "from": from_, "id": message_id, "timestamp": timestamp,
            "type": "text", "text": {"body": text},
        }]}}
    ]}]}


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(client: TestClient, message_id: str, from_: str, text: str):
    body = json.dumps(_payload(message_id, from_, text)).encode()
    return client.post(
        "/webhook", content=body,
        headers={"X-Hub-Signature-256": _sign(body), "Content-Type": "application/json"},
    )


def _build(
    on_create: Callable[[str], None] | None = None,
    *,
    calendar: Any = None,
    extractor: Any = None,
    client: Any = None,
):
    transport = FakeTransport()
    if client is None:
        client = _StubClient(on_create=on_create)
    app = build_serve_app(
        settings=WebhookSettings(app_secret=SECRET, verify_token=VERIFY_TOKEN),
        ruleset=RULES,
        catalogue=CATALOGUE,
        calendar=calendar if calendar is not None else MockCalendar(),
        client=client,
        # No script: every message extracts no facts, unless a test
        # passes its own. These wiring tests are mostly about a message
        # reaching the agent and a reply reaching the transport, not
        # about extraction itself -- tests/test_extract.py and the
        # end-to-end test below already cover that.
        extractor=extractor if extractor is not None else FakeExtractor(script={}),
        transport=transport,
        # No lifespan needed: these tests never touch the reaper or the
        # watchdog, which have their own tests in test_scheduler.py.
        enable_scheduler=False,
    )
    return app, transport, client


# --- the pipeline actually runs ----------------------------------------------

def test_an_inbound_webhook_produces_a_reply_through_a_stubbed_agent():
    app, transport, client = _build()
    tc = TestClient(app)

    r = _post(tc, "wamid.basic", "447700900001", "hi there")

    assert r.status_code == 200
    assert len(client.messages.calls) == 1
    sent = transport.sent()
    assert len(sent) == 1
    assert sent[0].to == "447700900001"
    assert sent[0].body == "ok: hi there"


# --- redelivery ---------------------------------------------------------------

def test_a_redelivered_message_id_produces_exactly_one_reply():
    app, transport, client = _build()
    tc = TestClient(app)

    _post(tc, "wamid.dup", "447700900002", "book me in")
    _post(tc, "wamid.dup", "447700900002", "book me in")

    assert len(transport.sent()) == 1
    assert len(client.messages.calls) == 1


def test_two_retries_arriving_concurrently_still_produce_exactly_one_reply():
    """The same message_id, delivered twice, at the same instant -- the
    way Meta's own retries can overlap. Dedup lives inside the job
    (see build_serve_app's docstring), guarded by the same per-thread
    lock that serializes a thread's jobs, so this must hold regardless
    of how the two requests interleave."""
    app, transport, client = _build()
    tc = TestClient(app)
    barrier = threading.Barrier(2)

    def fire():
        barrier.wait()
        _post(tc, "wamid.race", "447700900003", "book me in")

    threads = [threading.Thread(target=fire) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(transport.sent()) == 1
    assert len(client.messages.calls) == 1


# --- one thread's messages never interleave; two threads can overlap --------

def test_two_messages_on_one_thread_never_interleave():
    events: list[str] = []
    lock = threading.Lock()

    def on_create(text: str) -> None:
        tag = "first" if "first" in text else "second"
        with lock:
            events.append(f"{tag}-enter")
        time.sleep(0.05)
        with lock:
            events.append(f"{tag}-exit")

    app, transport, client = _build(on_create=on_create)
    tc = TestClient(app)
    barrier = threading.Barrier(2)
    same_thread = "447700900004"

    def post_first():
        barrier.wait()
        _post(tc, "wamid.first", same_thread, "first message")

    def post_second():
        barrier.wait()
        _post(tc, "wamid.second", same_thread, "second message")

    t1 = threading.Thread(target=post_first)
    t2 = threading.Thread(target=post_second)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Never interleaved: whichever job ran first, it fully exits before
    # the other one enters.
    assert events in (
        ["first-enter", "first-exit", "second-enter", "second-exit"],
        ["second-enter", "second-exit", "first-enter", "first-exit"],
    )
    assert len(transport.sent()) == 2
    assert len(client.messages.calls) == 2


def test_two_different_threads_can_overlap():
    events: list[str] = []
    lock = threading.Lock()

    def on_create(text: str) -> None:
        tag = "x" if "xthread" in text else "y"
        with lock:
            events.append(f"{tag}-enter")
        time.sleep(0.05)
        with lock:
            events.append(f"{tag}-exit")

    app, transport, client = _build(on_create=on_create)
    tc = TestClient(app)
    barrier = threading.Barrier(2)

    def post_x():
        barrier.wait()
        _post(tc, "wamid.x", "447700900005", "xthread message")

    def post_y():
        barrier.wait()
        _post(tc, "wamid.y", "447700900006", "ything message")

    t1 = threading.Thread(target=post_x)
    t2 = threading.Thread(target=post_y)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Both jobs must have entered before either exited -- that is what
    # "different threads do not block each other" means in terms of
    # recorded events, same as test_two_different_threads_jobs_can_overlap_in_time
    # in tests/test_conversation.py.
    first_exit_index = min(events.index("x-exit"), events.index("y-exit"))
    entered_before_any_exit = events[:first_exit_index]
    assert "x-enter" in entered_before_any_exit
    assert "y-enter" in entered_before_any_exit
    assert len(transport.sent()) == 2


# --- the request path returns before the job runs ----------------------------

def test_the_handler_returns_before_the_background_job_runs():
    """Prove ordering at the ASGI protocol level, not with a timer.

    Drives the app directly with a raw `send` callable and records the
    order two things happen in: the response body being sent, and the
    stubbed agent actually being called (which only happens inside the
    background job). If `on_message` still drained synchronously -- the
    bug this task exists to fix -- `job_ran` would appear first.
    """
    events: list[str] = []

    def on_create(text: str) -> None:
        events.append("job_ran")

    app, transport, client = _build(on_create=on_create)

    body = json.dumps(_payload("wamid.order", "447700900007", "order check")).encode()
    headers = [
        (b"x-hub-signature-256", _sign(body).encode()),
        (b"content-type", b"application/json"),
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/webhook",
        "raw_path": b"/webhook",
        "query_string": b"",
        "headers": headers,
        "client": ("testclient", 123),
        "server": ("testserver", 80),
    }

    delivered = False

    async def receive() -> dict[str, Any]:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    status: dict[str, int] = {}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
        elif message["type"] == "http.response.body":
            events.append("response_sent")

    asyncio.run(app(scope, receive, send))

    assert status["code"] == 200
    assert events == ["response_sent", "job_ran"]
    assert len(transport.sent()) == 1


# --- C1: the pipeline can actually book something end to end ----------------

def test_a_legitimate_colour_booking_succeeds_end_to_end_through_run_job():
    """The one test that would have caught C1: with a real extractor
    wired in but stubbed, and a stubbed model, a straightforward colour
    booking must actually go through the webhook, the extractor, the
    agent's tool loop, and the gate, and land as a real booking on the
    calendar -- not just as a text reply.

    Before this task's fix, run_job called `state.add_facts({}, ...)`
    with no extractor, so `is_first_colour_visit` (etc.) never reached
    `conversation.facts` and every colour request blocked with "we never
    established is_first_colour_visit". This test fails against that bug
    and passes against the fix.
    """
    slot_id = "s_e2e_colour"
    # A real wall-clock time, comfortably in the future -- run_job reads
    # datetime.now(timezone.utc) itself, so this cannot be pinned to a
    # fixed `now` the way tests/test_tools.py can. Nudged off Sunday: the
    # policy's no_colour_on_sunday rule now reads the slot's own weekday
    # (see wca.tools.request_booking), not a model-supplied fact, so a
    # slot that happened to land on a Sunday would make this test flaky.
    starts_at = datetime.now(timezone.utc) + timedelta(days=30)
    while starts_at.weekday() == 6:  # Sunday
        starts_at += timedelta(days=1)
    calendar = MockCalendar(slots=[Slot(slot_id, starts_at)])

    # requested_weekday is no longer a field the model reports -- it is
    # derived in wca.tools.request_booking from the slot's own starts_at
    # (see wca.tools.DERIVED_FACTS). Only customer-describing facts are
    # supplied here now.
    extractor = FakeExtractor(script={
        "wamid.e2e_booking": RawFactSet(
            is_first_colour_visit=False, customer_is_over_16=True,
        ),
    })
    client = _ScriptedClient(script=[
        _tool_call("request_booking", {"service_id": "svc_colour_full", "slot_id": slot_id}),
        _final_text("You're all booked in for your colour."),
    ])

    app, transport, _ = _build(calendar=calendar, extractor=extractor, client=client)
    tc = TestClient(app)

    r = _post(tc, "wamid.e2e_booking", "447700900010", "I'd like a full colour please")

    assert r.status_code == 200
    sent = transport.sent()
    assert len(sent) == 1
    assert sent[0].body == "You're all booked in for your colour."
    assert len(calendar.bookings()) == 1
    assert calendar.bookings()[0].slot_id == slot_id


# --- C3: never leave the customer with silence -------------------------------

def test_an_empty_agent_reply_still_produces_exactly_one_outbound_message():
    """Hitting MAX_ITERATIONS with no final text, or any other path that
    leaves `agent.run_turn` returning "", must not leave the customer
    with nothing."""
    client = _ScriptedClient(script=[_no_text_reply()])
    app, transport, _ = _build(client=client)
    tc = TestClient(app)

    r = _post(tc, "wamid.empty_reply", "447700900011", "hello?")

    assert r.status_code == 200
    sent = transport.sent()
    assert len(sent) == 1
    assert sent[0].body  # non-empty: the fallback, not silence


def test_an_unexpected_failure_in_the_job_still_sends_one_fallback_message():
    """A job that raises partway through (a model outage, here simulated)
    must still leave the customer with a reply, and the error must still
    surface to the caller so it is visible in logs and in the queue's
    error list -- run_job re-raises after sending the fallback."""
    app, transport, _ = _build(client=_RaisingClient())
    tc = TestClient(app)

    r = _post(tc, "wamid.job_failure", "447700900012", "book me in")

    assert r.status_code == 200  # the webhook itself always answers Meta
    sent = transport.sent()
    assert len(sent) == 1
    assert sent[0].body  # the fallback, not silence


# --- Cause 1: the extractor receives role-labelled history, not flat text ---

def test_cli_hands_the_extractor_role_labelled_history_not_flat_strings():
    """Before this fix, wca.cli built thread_history with roles and then
    called the extractor with `[turn["content"] for turn in
    thread_history]`, throwing the roles away. Proves the extractor now
    receives the exact same {"role": ..., "content": ...} shape
    thread_history carries -- what prompts/extract-v0.1.md's role
    attribution (Cause 2) depends on."""
    seen: list[list[dict[str, str]]] = []

    class _RecordingExtractor:
        version = "recording-v0"

        def extract(self, message_id: str, text: str, thread: Any) -> ExtractionResult:
            seen.append(list(thread))
            return ExtractionResult(message_id=message_id, facts={})

    app, transport, client = _build(extractor=_RecordingExtractor())
    tc = TestClient(app)
    same_thread = "447700900030"

    _post(tc, "wamid.role_1", same_thread, "hi")
    _post(tc, "wamid.role_2", same_thread, "book me a cut")

    assert seen[0] == []
    assert seen[1] == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok: hi"},
    ]
    assert all(set(turn) == {"role", "content"} for turn in seen[1])


# --- Cause 3: the ask-loop guard escalates instead of asking a third time ---

def test_a_fact_asked_about_twice_with_no_answer_escalates_on_the_third_message():
    """Regression test for the live thread this task exists to fix: the
    salon owner said "yes" to "are you 16 or over?" and the agent kept
    asking anyway, four times. wca.cli.MAX_ASKS_PER_FACT caps how many
    times one thread can ask about the same customer-only fact
    (wca.tools.CONVERSATIONAL_FACTS), independent of anything the model
    says. Here the extractor never returns the fact (the worst case: not
    even an accepted answer reaches conversation.facts) and the model
    always just asks a question in plain text -- no tool call at all, so
    the guard cannot be relying on a request_booking audit trail. The
    third message must escalate rather than ask a third time, and the
    model must not even be called for it -- the scripted client only
    carries two responses, so a third call raises AssertionError."""
    extractor = FakeExtractor(script={
        "wamid.ask_1": RawFactSet(service_category="colour", is_first_colour_visit=False),
        # Every later message: no facts at all, however the customer
        # actually answered.
    })
    client = _ScriptedClient(script=[
        _final_text("Are you 16 or over?"),
        _final_text("Sorry -- are you under 16, or 16 and over?"),
    ])
    app, transport, _ = _build(extractor=extractor, client=client)
    tc = TestClient(app)
    same_thread = "447700900031"

    _post(tc, "wamid.ask_1", same_thread, "I'd like a full colour please")
    _post(tc, "wamid.ask_2", same_thread, "yes")
    r = _post(tc, "wamid.ask_3", same_thread, "yes, I'm over 16")

    assert r.status_code == 200
    assert len(client.messages.calls) == 2  # never called a third time
    sent = transport.sent()
    assert len(sent) == 3
    assert sent[0].body == "Are you 16 or over?"
    assert sent[1].body == "Sorry -- are you under 16, or 16 and over?"
    import wca.cli as cli
    assert sent[2].body == cli.FALLBACK_REPLY  # escalated, not asked a third time


def test_answering_the_fact_resets_the_ask_counter():
    """The guard must not fire just because a fact was asked about once
    or twice earlier in the conversation -- only when it is STILL
    missing. Once the extractor actually reports the fact, the counter
    resets and a later, unrelated question does not trip the guard."""
    extractor = FakeExtractor(script={
        "wamid.reset_1": RawFactSet(service_category="colour"),
        "wamid.reset_2": RawFactSet(),
        "wamid.reset_3": RawFactSet(customer_is_over_16=True, is_first_colour_visit=False),
        "wamid.reset_4": RawFactSet(),
        "wamid.reset_5": RawFactSet(),
        "wamid.reset_6": RawFactSet(),
    })
    client = _ScriptedClient(script=[_final_text("ok") for _ in range(6)])
    app, transport, _ = _build(extractor=extractor, client=client)
    tc = TestClient(app)
    same_thread = "447700900032"

    for i in range(1, 7):
        r = _post(tc, f"wamid.reset_{i}", same_thread, f"message {i}")
        assert r.status_code == 200

    # All six reached the model -- the fact was resolved on message 3,
    # before the two-strikes cap on the (now irrelevant) earlier asks
    # could trip.
    assert len(client.messages.calls) == 6


# --- I7: a per-sender budget bounds spend -------------------------------------

def test_a_thread_over_its_message_budget_stops_calling_the_model(monkeypatch):
    import wca.cli as cli

    monkeypatch.setattr(cli, "MAX_MESSAGES_PER_WINDOW", 2)

    app, transport, client = _build()
    tc = TestClient(app)
    same_thread = "447700900013"

    for i in range(4):
        _post(tc, f"wamid.budget_{i}", same_thread, f"message {i}")

    # Only the first two (the budget) ever reached the model.
    assert len(client.messages.calls) == 2
    # Every message still gets exactly one reply -- the last two are the
    # fallback, not silence.
    assert len(transport.sent()) == 4
    assert transport.sent()[-1].body == cli.FALLBACK_REPLY
    assert transport.sent()[-2].body == cli.FALLBACK_REPLY


# --- outbound interactive messages: slots -> list, no choices -> text -------

def _future_slots(n: int, *, start_days: int = 2, hour: int = 14) -> list[Slot]:
    """`n` real, timezone-aware slots inside check_availability's default
    14-day lookahead window, far enough out to dodge lead-time rules."""
    base = datetime.now(timezone.utc) + timedelta(days=start_days)
    return [
        Slot(f"s_offer_{i}", (base + timedelta(days=i)).replace(
            hour=hour, minute=0, second=0, microsecond=0
        ))
        for i in range(n)
    ]


def test_offering_slots_sends_a_list_message_with_the_tools_own_labels():
    """check_availability's own results become the list's rows, verbatim
    -- see wca.cli._interactive_offer. Never reformatted, never a subset
    the model chose to mention in its own words."""
    slots = _future_slots(3)
    calendar = MockCalendar(slots=slots)
    expected_labels = [human_slot_label(s.starts_at) for s in slots]

    client = _ScriptedClient(script=[
        _tool_call("check_availability", {"service_id": "svc_cut"}),
        _final_text("Here are a few times that work for a cut."),
    ])
    app, transport, _ = _build(calendar=calendar, client=client)
    tc = TestClient(app)

    r = _post(tc, "wamid.offer_slots", "447700900040", "when can I get a cut?")

    assert r.status_code == 200
    sent = transport.sent()
    assert len(sent) == 1
    assert sent[0].list_rows == tuple(expected_labels)
    assert sent[0].buttons == ()
    # The body carries the exact same text the customer would have read
    # as plain prose -- the interactive send is not a second channel.
    assert sent[0].body == "Here are a few times that work for a cut."


def test_eleven_slots_falls_back_to_plain_text_not_a_raise_or_a_truncation():
    slots = _future_slots(11)
    calendar = MockCalendar(slots=slots)

    client = _ScriptedClient(script=[
        _tool_call("check_availability", {"service_id": "svc_cut"}),
        _final_text("Lots of options this week for a cut."),
    ])
    app, transport, _ = _build(calendar=calendar, client=client)
    tc = TestClient(app)

    r = _post(tc, "wamid.offer_eleven", "447700900041", "when can I get a cut?")

    assert r.status_code == 200
    sent = transport.sent()
    assert len(sent) == 1
    # Fell back to plain text -- no list, no truncated 10-row list either.
    assert sent[0].list_rows == ()
    assert sent[0].buttons == ()
    assert sent[0].body == "Lots of options this week for a cut."


def test_a_turn_with_no_choices_still_sends_plain_text():
    """No tool ran, so there is nothing for _interactive_offer to build an
    offer out of -- the reply goes out exactly as it always has."""
    app, transport, _ = _build()
    tc = TestClient(app)

    r = _post(tc, "wamid.no_choices", "447700900042", "hi there")

    assert r.status_code == 200
    sent = transport.sent()
    assert len(sent) == 1
    assert sent[0].list_rows == ()
    assert sent[0].buttons == ()
    assert sent[0].body == "ok: hi there"
