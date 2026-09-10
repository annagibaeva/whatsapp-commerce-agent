"""What we know about one conversation.

`last_inbound_at` is the timestamp the 24-hour window is measured from.
Every inbound message moves it.

`ConversationState` is a plain Pydantic object with no write-through: a
`get_or_create(...).add_facts(...)` call mutates the object in place but
does not durably save it on its own. `SqliteConversationStore` (below)
exposes an explicit `save(state)` for that -- the call site is
`cli.py::run_job`, right after `state.add_facts(...)`. `ConversationStore`
(the in-memory one) gets a no-op `save` too, so both stores satisfy the
same shape and `run_job` never has to know which one it has.
"""

from __future__ import annotations

import json
import sqlite3
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

    def save(self, state: ConversationState) -> None:
        """No-op: `self._threads[state.thread_id]` already *is* `state`
        (the same object `get_or_create` handed back), so any mutation
        the caller made is already visible. Exists only so a call site
        that does not know whether it holds an in-memory or a durable
        store can call `save` unconditionally -- see `SqliteConversationStore
        .save`, where this matters."""
        self._threads[state.thread_id] = state


class SqliteConversationStore:
    """Same contract as `ConversationStore`, backed by the `conversations`
    table (see `wca.db`).

    Unlike the in-memory store, mutating the `ConversationState` object
    `get_or_create`/`get` hands back does **not** persist anything --
    there is no shared object identity across a restart. `save(state)`
    is the explicit write-through this store adds to the shape;
    `cli.py::run_job` calls it right after `state.add_facts(...)`.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_or_create(self, thread_id: str, now: datetime) -> ConversationState:
        existing = self.get(thread_id)
        if existing is not None:
            return existing
        state = ConversationState(thread_id=thread_id, last_inbound_at=now)
        self.save(state)
        return state

    def get(self, thread_id: str) -> ConversationState | None:
        row = self._conn.execute(
            "SELECT last_inbound_at, facts_json, hold_id FROM conversations WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        if row is None:
            return None
        last_inbound_at, facts_json, hold_id = row
        return ConversationState(
            thread_id=thread_id,
            last_inbound_at=datetime.fromisoformat(last_inbound_at),
            facts=json.loads(facts_json),
            hold_id=hold_id,
        )

    def save(self, state: ConversationState) -> None:
        self._conn.execute(
            """
            INSERT INTO conversations (thread_id, last_inbound_at, facts_json, hold_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET
                last_inbound_at = excluded.last_inbound_at,
                facts_json = excluded.facts_json,
                hold_id = excluded.hold_id
            """,
            (
                state.thread_id,
                state.last_inbound_at.isoformat(),
                json.dumps(state.facts),
                state.hold_id,
            ),
        )
        self._conn.commit()
