"""A transport that goes nowhere.

Every automated test uses this. It is what lets the suite run with no
network, no token and no tunnel.
"""

from __future__ import annotations

from wca.transport.base import InboundMessage, OutboundMessage

#: WhatsApp allows three reply buttons. More has to be a list message.
MAX_BUTTONS = 3


class FakeTransport:
    def __init__(self) -> None:
        self._sent: list[OutboundMessage] = []
        self._inbox: list[InboundMessage] = []

    def send_text(self, to: str, body: str) -> OutboundMessage:
        message = OutboundMessage(to=to, body=body)
        self._sent.append(message)
        return message

    def send_buttons(self, to: str, body: str, labels: list[str]) -> OutboundMessage:
        if len(labels) > MAX_BUTTONS:
            raise ValueError(f"WhatsApp allows at most three buttons, got {len(labels)}")
        message = OutboundMessage(to=to, body=body, buttons=tuple(labels))
        self._sent.append(message)
        return message

    def receive(self, message: InboundMessage) -> None:
        self._inbox.append(message)

    def sent(self) -> tuple[OutboundMessage, ...]:
        return tuple(self._sent)

    def inbox(self) -> tuple[InboundMessage, ...]:
        return tuple(self._inbox)
