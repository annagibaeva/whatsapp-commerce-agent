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
from typing import Any, Callable

from fastapi.testclient import TestClient

from wca.calendar.mock import MockCalendar
from wca.catalogue import load_catalogue
from wca.cli import build_serve_app
from wca.rules.store import load_ruleset
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


def _build(on_create: Callable[[str], None] | None = None):
    transport = FakeTransport()
    client = _StubClient(on_create=on_create)
    app = build_serve_app(
        settings=WebhookSettings(app_secret=SECRET, verify_token=VERIFY_TOKEN),
        ruleset=RULES,
        catalogue=CATALOGUE,
        calendar=MockCalendar(),
        client=client,
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
