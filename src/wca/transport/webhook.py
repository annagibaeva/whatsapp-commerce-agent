"""The webhook endpoint.

Two things here are security, not plumbing.

Check the signature. Meta signs every webhook with your app secret. If we
skip that check, anyone who finds this URL can post a fake customer
message and get an appointment booked.

Use the raw bytes. Parsing the JSON and serialising it again changes
whitespace and key order. The hash then never matches and every real
message gets rejected.

Reply fast. Meta retries a webhook it does not get a quick answer to, and
retries mean duplicate messages. This handler checks the signature, hands
the message on, and returns. It never waits for a model call.

A third pair of endpoints lives here too: `GET /reminders/due` and
`POST /reminders/sent`, polled by a cloud-hosted n8n instance on a
schedule to send the 24-hour-ahead confirmation reminder. n8n is the
scheduler and the sender; this process is only ever the source of truth
about what is due. **We never call n8n** -- there is no outbound
webhook, no push, no timer that calls out, only these two endpoints for
n8n to poll. Same trust posture as the WhatsApp webhook above: this URL
is public, so a shared secret gates both, checked with
`hmac.compare_digest` before anything else runs.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any, Callable

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, SecretStr

from wca.tools import human_slot_label
from wca.transport.base import InboundMessage
from wca.transport.whatsapp import parse_inbound

#: Header n8n sends the shared secret in, for both reminder endpoints.
REMINDER_SECRET_HEADER = "X-N8N-Secret"

#: `check_availability`'s own default look-ahead is 14 days of slots to
#: browse; a reminder window is a much narrower thing -- "roughly 24
#: hours away" per the PRD's n8n scheduling hop. 26 gives the poll some
#: slack either side of the 24-hour mark without it drifting into
#: "everything upcoming".
DEFAULT_REMINDER_WINDOW_HOURS = 26.0

#: A sane ceiling on `?within_hours=`, so a misconfigured n8n workflow
#: (or a stray query param) cannot turn this into "every booking on the
#: books" -- one week is generous for a same-day scheduling hop and still
#: nowhere near unbounded.
MAX_REMINDER_WINDOW_HOURS = 168.0


class WebhookSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    app_secret: SecretStr
    verify_token: str
    #: Shared secret n8n sends in `X-N8N-Secret`. `None` means the
    #: operator never set `N8N_REMINDER_SECRET` in the environment --
    #: `verify_reminder_secret` below treats that as closed, not open:
    #: every request to either reminder endpoint is refused, not let
    #: through unauthenticated. Optional (unlike `app_secret` and
    #: `verify_token`, both required) so a deployment that has not yet
    #: turned the n8n hop on can still build `WebhookSettings` and serve
    #: the WhatsApp webhook exactly as before.
    reminder_secret: SecretStr | None = None

    def __repr__(self) -> str:
        # SecretStr already keeps app_secret out of pydantic's default
        # field-by-field repr, and (unlike a __repr__-only guard on a
        # plain class such as WhatsAppTransport) out of str(settings),
        # f"{settings}", model_dump() and JSON serialisation too, since
        # a pydantic BaseModel defines __str__ separately from __repr__
        # and both walk every field. This override is belt-and-braces:
        # it also keeps verify_token — not itself the secret this fix
        # targets, but not useful in a log line either — out of ad hoc
        # prints, matching the precedent it was copied from.
        return "WebhookSettings(app_secret=***, verify_token=***, reminder_secret=***)"

    __str__ = __repr__


def verify_signature(app_secret: SecretStr, raw_body: bytes, header: str | None) -> bool:
    """Constant-time check of the X-Hub-Signature-256 header."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(
        app_secret.get_secret_value().encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])


def verify_reminder_secret(reminder_secret: SecretStr | None, header: str | None) -> bool:
    """Constant-time check of the `X-N8N-Secret` header.

    `reminder_secret is None` means `N8N_REMINDER_SECRET` was never set
    in the environment -- refused unconditionally, the same "missing
    secret is closed, not open" rule `cmd_serve` already applies to
    `WHATSAPP_APP_SECRET`. An empty or missing `header` is refused before
    `compare_digest` ever runs: comparing two empty strings with
    `compare_digest` returns `True`, which would otherwise turn "no
    header sent" into "authenticated".
    """
    if reminder_secret is None or not header:
        return False
    return hmac.compare_digest(reminder_secret.get_secret_value(), header)


def create_app(
    settings: WebhookSettings,
    on_message: Callable[[InboundMessage], None],
    after_message: Callable[[InboundMessage], None] | None = None,
    lifespan: Callable[[FastAPI], Any] | None = None,
    calendar: Any | None = None,
    catalogue: Any | None = None,
    now: Callable[[], datetime] | None = None,
) -> FastAPI:
    """Build the webhook app.

    `on_message` runs synchronously, in the request, for every parsed
    message -- it must stay fast (see the module docstring: reply fast,
    never wait for a model call). `after_message`, if given, is scheduled
    as a FastAPI `BackgroundTasks` job per message instead: it runs after
    the response has been sent, which is where slow work (draining a
    conversation's queue, which can call a model) belongs. Left `None`,
    nothing changes about the request beyond `on_message` itself --
    existing callers that only need synchronous delivery are unaffected.

    `lifespan` is passed straight through to `FastAPI(...)`, so `serve`
    can start and stop the reaper and watchdog timers around the app's
    life without this module knowing anything about schedulers.

    `calendar` and `catalogue` back the two `/reminders/*` routes below;
    left `None`, the WhatsApp webhook routes work exactly as before and
    the reminder routes answer 500 rather than raise, since nothing here
    requires a caller who only wants the webhook to supply either.

    `now` is this module's one deliberate exception to "library code
    never calls `datetime.now()`" (see `wca.clock`): a route handler is
    the edge, the same place `wca.cli`'s `run_job` and its scheduler
    closures read the wall clock, and `/reminders/due` and
    `/reminders/sent` cannot answer "due" or stamp a reminder without
    reading the time from *somewhere*. Left `None`, that somewhere is
    `datetime.now(timezone.utc)`, read fresh on every request. Tests
    pass a fixed callable instead -- `lambda: NOW` -- to get the same
    deterministic control over "due" that `wca.clock.SimulatedClock`
    gives every other timer in this codebase, with no `time.sleep` and
    no flake.
    """
    app = FastAPI(title="WhatsApp Commerce Agent webhook", lifespan=lifespan)
    clock: Callable[[], datetime] = now or (lambda: datetime.now(timezone.utc))

    @app.get("/webhook")
    def handshake(request: Request) -> Response:
        params = request.query_params
        if params.get("hub.verify_token") != settings.verify_token:
            return Response(status_code=403)
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")

    @app.post("/webhook")
    async def receive(request: Request, background_tasks: BackgroundTasks) -> Response:
        raw = await request.body()
        header = request.headers.get("X-Hub-Signature-256")
        if not verify_signature(settings.app_secret, raw, header):
            # Rejected before anything parses it. The reason is for
            # operators: Meta retries a 403, and "header missing" vs
            # "hash mismatch" is the difference between Cloudflare
            # stripping the header and WHATSAPP_APP_SECRET not matching
            # the app that signed the body. Never log the secret, the
            # signature, or the body — only lengths and the reason.
            if not header:
                reason = "header missing"
            elif not header.startswith("sha256="):
                reason = "header not sha256="
            else:
                reason = "hash mismatch"
            secret_len = len(settings.app_secret.get_secret_value())
            print(
                f"webhook 403: {reason} "
                f"(secret_len={secret_len}, body_len={len(raw)})"
            )
            return Response(status_code=403)

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return Response(status_code=200)

        for message in parse_inbound(payload):
            on_message(message)
            if after_message is not None:
                background_tasks.add_task(after_message, message)

        return Response(status_code=200)

    @app.get("/reminders/due")
    def reminders_due(request: Request, within_hours: float = DEFAULT_REMINDER_WINDOW_HOURS) -> Response:
        # Same posture as /webhook's signature check: reject before doing
        # any work, including before looking at `within_hours` or
        # touching the calendar.
        header = request.headers.get(REMINDER_SECRET_HEADER)
        if not verify_reminder_secret(settings.reminder_secret, header):
            return Response(status_code=401)
        if calendar is None or catalogue is None:
            return Response(status_code=500)

        window = max(0.0, min(within_hours, MAX_REMINDER_WINDOW_HOURS))
        current = clock()

        due = []
        for booking in calendar.due_reminders(window, current):
            slot = calendar.slot(booking.slot_id)
            starts_at = slot.starts_at if slot is not None else None
            service = catalogue.get(booking.service_id) if booking.service_id else None
            due.append({
                "booking_id": booking.booking_id,
                "customer_phone": booking.thread_id,
                "service_name": service.name if service is not None else None,
                "starts_at": starts_at.isoformat() if starts_at is not None else None,
                "starts_at_label": human_slot_label(starts_at) if starts_at is not None else None,
            })
        return JSONResponse(content=due)

    @app.post("/reminders/sent")
    async def reminders_sent(request: Request) -> Response:
        header = request.headers.get(REMINDER_SECRET_HEADER)
        if not verify_reminder_secret(settings.reminder_secret, header):
            return Response(status_code=401)
        if calendar is None:
            return Response(status_code=500)

        raw = await request.body()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return Response(status_code=400)
        booking_id = payload.get("booking_id") if isinstance(payload, dict) else None
        if not booking_id:
            return Response(status_code=400)

        booking = calendar.mark_reminded(booking_id, now=clock())
        if booking is None:
            return Response(status_code=404)
        return JSONResponse(content={
            "ok": True,
            "booking_id": booking.booking_id,
            "reminder_sent_at": booking.reminder_sent_at.isoformat(),
        })

    return app
