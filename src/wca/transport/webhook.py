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
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Callable

from fastapi import FastAPI, Request, Response
from pydantic import BaseModel, ConfigDict, SecretStr

from wca.transport.base import InboundMessage
from wca.transport.whatsapp import parse_inbound


class WebhookSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    app_secret: SecretStr
    verify_token: str

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
        return "WebhookSettings(app_secret=***, verify_token=***)"

    __str__ = __repr__


def verify_signature(app_secret: SecretStr, raw_body: bytes, header: str | None) -> bool:
    """Constant-time check of the X-Hub-Signature-256 header."""
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(
        app_secret.get_secret_value().encode(), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):])


def create_app(
    settings: WebhookSettings,
    on_message: Callable[[InboundMessage], None],
) -> FastAPI:
    app = FastAPI(title="WhatsApp Commerce Agent webhook")

    @app.get("/webhook")
    def handshake(request: Request) -> Response:
        params = request.query_params
        if params.get("hub.verify_token") != settings.verify_token:
            return Response(status_code=403)
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")

    @app.post("/webhook")
    async def receive(request: Request) -> Response:
        raw = await request.body()
        if not verify_signature(
            settings.app_secret, raw, request.headers.get("X-Hub-Signature-256")
        ):
            # Rejected before anything parses it.
            return Response(status_code=403)

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return Response(status_code=200)

        for message in parse_inbound(payload):
            on_message(message)

        return Response(status_code=200)

    return app
