"""What we know about one conversation.

`last_inbound_at` is the timestamp the 24-hour window is measured from.
Every inbound message moves it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ConversationState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thread_id: str
    last_inbound_at: datetime
    facts: dict[str, Any] = Field(default_factory=dict)
    hold_id: str | None = None

    def add_facts(self, new: dict[str, Any], now: datetime) -> None:
        """Merge in new facts. A later message wins on a conflict."""
        self.facts.update(new)
        self.last_inbound_at = now


class ConversationStore:
    def __init__(self) -> None:
        self._threads: dict[str, ConversationState] = {}

    def get_or_create(self, thread_id: str, now: datetime) -> ConversationState:
        if thread_id not in self._threads:
            self._threads[thread_id] = ConversationState(
                thread_id=thread_id, last_inbound_at=now
            )
        return self._threads[thread_id]

    def get(self, thread_id: str) -> ConversationState | None:
        return self._threads.get(thread_id)
