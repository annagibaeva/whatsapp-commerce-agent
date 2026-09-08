"""Talking to the WhatsApp Cloud API.

This file and webhook.py are the only two that know WhatsApp exists.

Check GRAPH_VERSION against the curl on your API Setup page. Meta moves
it, and an old version fails in ways that look like a bug in this code.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx

from wca.transport.base import InboundMessage, OutboundMessage

GRAPH_VERSION = "v23.0"
MAX_BUTTONS = 3


def _visible_text(message: dict[str, Any]) -> str | None:
    """What the customer typed, tapped, or picked -- or None to skip.

    Three shapes carry visible text today:

    - a plain text message: the typed body.
    - a quick-reply button tap on a template (type "button"): the
      button's own label, e.g. Confirm/Reschedule on the 24-hour
      appointment reminder.
    - a reply to an interactive message (type "interactive"), which
      Meta splits into two sub-shapes: `button_reply` (tapping a
      button we sent) and `list_reply` (picking a row from a list we
      sent). Both carry a `title` -- the label the customer saw and
      chose.

    Any other type, or one of these three with its inner object missing
    or empty, returns None so the caller skips it instead of raising.
    """
    msg_type = message.get("type")
    if msg_type == "text":
        return message.get("text", {}).get("body")
    if msg_type == "button":
        return message.get("button", {}).get("text")
    if msg_type == "interactive":
        interactive = message.get("interactive", {})
        sub_type = interactive.get("type")
        if sub_type == "button_reply":
            return interactive.get("button_reply", {}).get("title")
        if sub_type == "list_reply":
            return interactive.get("list_reply", {}).get("title")
        return None
    return None


def parse_inbound(payload: dict[str, Any]) -> list[InboundMessage]:
    """Pull customer-visible messages out of a webhook body.

    Meta sends delivery statuses through the same webhook. Those carry no
    message and must be ignored, not treated as empty text. A tap on a
    template's quick-reply button, or a reply to an interactive button
    or list message, is turned into the same shape as a typed message:
    its `text` is what the customer visibly chose, so the agent and the
    extractor never have to learn a new shape to answer a button tap.
    Anything else -- an image, a location, a reaction -- is still
    ignored: v0 only reads what the customer could have typed instead.
    """
    out: list[InboundMessage] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                text = _visible_text(message)
                if text is None:
                    continue
                out.append(InboundMessage(
                    message_id=message["id"],
                    thread_id=message["from"],
                    text=text,
                    sent_at=datetime.fromtimestamp(
                        int(message["timestamp"]), tz=timezone.utc
                    ),
                ))
    return out


class WhatsAppTransport:
    def __init__(
        self,
        phone_number_id: str,
        access_token: str,
        http: Any | None = None,
        graph_version: str = GRAPH_VERSION,
    ) -> None:
        self._phone_number_id = phone_number_id
        self._token = access_token
        self._http = http or httpx.Client(timeout=10.0)
        self._base = f"https://graph.facebook.com/{graph_version}/{phone_number_id}/messages"

    def __repr__(self) -> str:
        # Never put the token in a repr. Reprs end up in logs.
        return f"WhatsAppTransport(phone_number_id={self._phone_number_id!r})"

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        response = self._http.post(
            self._base,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
            json=body,
            timeout=10.0,
        )
        response.raise_for_status()
        return response.json()

    def send_text(self, to: str, body: str) -> OutboundMessage:
        self._post({
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": body},
        })
        return OutboundMessage(to=to, body=body)

    def send_buttons(self, to: str, body: str, labels: list[str]) -> OutboundMessage:
        if len(labels) > MAX_BUTTONS:
            raise ValueError(f"WhatsApp allows at most three buttons, got {len(labels)}")
        self._post({
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body},
                "action": {"buttons": [
                    {"type": "reply", "reply": {"id": f"btn_{i}", "title": label}}
                    for i, label in enumerate(labels)
                ]},
            },
        })
        return OutboundMessage(to=to, body=body, buttons=tuple(labels))
