"""What happened and why.

Append only. Each record stores the proposal, the verdict, the rules in
English, the ruleset version that was live, and what the gate read. That
last part matters: if Meta later refuses to send a message check 6 said
was deliverable, the record shows what we believed at the time.
"""

from __future__ import annotations

import json

from wca.models import AuditRecord


class AuditLog:
    def __init__(self) -> None:
        self._records: list[AuditRecord] = []
        self._seen: set[str] = set()

    def append(self, record: AuditRecord) -> bool:
        """Add a record. Returns False if this proposal is already in."""
        key = record.proposal.proposal_id
        if key in self._seen:
            return False
        self._seen.add(key)
        self._records.append(record)
        return True

    def records(self) -> tuple[AuditRecord, ...]:
        return tuple(self._records)

    def to_json(self) -> str:
        return json.dumps([r.model_dump(mode="json") for r in self._records], indent=2)

    def add_turn_cost(self, count_before: int, cost_usd: float) -> None:
        """Add `cost_usd` to `turn_cost_usd` on every record appended
        since `count_before` (an index -- take it from `len(audit)`
        *before* the turn starts).

        This exists because a record can be created before a turn's full
        cost is known: `wca.tools.request_booking`/`escalate` build their
        `AuditRecord` mid-turn, while `wca.agent.Agent` may still make
        another model call afterwards (the final, tool-free response that
        ends the loop). The caller -- `wca.cli.run_job` -- calls this once
        the turn is actually over, with that turn's true total
        (extraction cost plus `Agent.cost_usd`).

        `AuditRecord` is frozen, so each record in range is replaced with
        `model_copy`, not mutated. Adds rather than sets, in case a
        single turn already put more than one record here (e.g. an
        `escalate` call followed by a `request_booking` call) -- each
        gets the same turn total, not a split of it.
        """
        for i in range(count_before, len(self._records)):
            record = self._records[i]
            so_far = record.turn_cost_usd or 0.0
            self._records[i] = record.model_copy(update={"turn_cost_usd": so_far + cost_usd})

    def cost_for_thread(self, thread_id: str) -> float:
        """Total cost recorded for one thread, across every turn that
        left an audit record.

        A booking is not one record's cost -- `proposal.thread_id` may
        carry several records across a conversation (an earlier
        `request_booking` refused by the gate, an `escalate` that was
        itself refused, the eventual successful `request_booking`), each
        stamped with its own turn's `turn_cost_usd`. This sums all of
        them for the given thread; a record with no `turn_cost_usd`
        (never stamped -- see `add_turn_cost`) contributes nothing rather
        than raising.
        """
        return sum(
            record.turn_cost_usd or 0.0
            for record in self._records
            if record.proposal.thread_id == thread_id
        )

    def __len__(self) -> int:
        return len(self._records)
