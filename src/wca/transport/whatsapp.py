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


def parse_inbound(payload: dict[str, Any]) -> list[InboundMessage]:
    """Pull text messages out of a webhook body.

    Meta sends delivery statuses through the same webhook. Those carry no
    message and must be ignored, not treated as empty text. Non-text
    messages are ignored too: v0 reads text only.
    """
    out: list[InboundMessage] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                if message.get("type") != "text":
                    continue
                body = message.get("text", {}).get("body")
                if body is None:
                    continue
                out.append(InboundMessage(
                    message_id=message["id"],
                    thread_id=message["from"],
                    text=body,
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
