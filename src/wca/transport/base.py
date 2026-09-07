"""What any transport has to do.

Only this package knows WhatsApp exists. Everything above it runs against
the fake.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class InboundMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    message_id: str
    thread_id: str
    text: str
    sent_at: datetime


class OutboundMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    to: str
    body: str
    buttons: tuple[str, ...] = ()


class TransportPort(Protocol):
    def send_text(self, to: str, body: str) -> OutboundMessage: ...

    def send_buttons(self, to: str, body: str, labels: list[str]) -> OutboundMessage: ...
