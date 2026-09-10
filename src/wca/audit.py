"""What happened and why.

Append only. Each record stores the proposal, the verdict, the rules in
English, the ruleset version that was live, and what the gate read. That
last part matters: if Meta later refuses to send a message check 6 said
was deliverable, the record shows what we believed at the time.

`SqliteAuditLog` alongside `AuditLog`: same public surface, backed by
the `audit_records` table (see `wca.db`) instead of a list and a
Python-side `set()`. That set is exactly the bug the append-dedup
contract exists to protect against -- see `test_dedup_survives
_reopening_the_connection` in tests/test_audit.py. `AuditLog` (the
in-memory one) is still what the test suite injects by default; the
sqlite one is what `build_serve_app` uses when a `--db` path is given
(Task 8).
"""

from __future__ import annotations

import json
import sqlite3

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


class SqliteAuditLog:
    """Same contract as `AuditLog`, backed by the `audit_records` table.

    Dedup is a `SELECT` against the table's own `PRIMARY KEY`, not a
    Python `set()` -- that is what makes `append` return `False` for a
    repeat `proposal_id` on a freshly-constructed instance pointed at a
    db file that already has the row, not only within one process's
    lifetime. `AuditRecord` round-trips through `record_json` via
    `model_dump_json()`/`model_validate_json()`, so there is no manual
    field mapping here to drift out of sync with `models.py`.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def append(self, record: AuditRecord) -> bool:
        key = record.proposal.proposal_id
        existing = self._conn.execute(
            "SELECT 1 FROM audit_records WHERE proposal_id = ?", (key,)
        ).fetchone()
        if existing is not None:
            return False
        self._conn.execute(
            "INSERT INTO audit_records (proposal_id, record_json, decided_at) VALUES (?, ?, ?)",
            (key, record.model_dump_json(), record.decided_at.isoformat()),
        )
        self._conn.commit()
        return True

    def records(self) -> tuple[AuditRecord, ...]:
        rows = self._conn.execute(
            "SELECT record_json FROM audit_records ORDER BY rowid"
        ).fetchall()
        return tuple(AuditRecord.model_validate_json(row[0]) for row in rows)

    def to_json(self) -> str:
        return json.dumps([r.model_dump(mode="json") for r in self.records()], indent=2)

    def add_turn_cost(self, count_before: int, cost_usd: float) -> None:
        """Same contract as `AuditLog.add_turn_cost` -- see its docstring.

        Records are addressed by insertion order (`rowid`), same as the
        in-memory list's own index order, so `count_before` (taken from
        `len(audit)` before the turn) means the same thing against either
        implementation.
        """
        records = self.records()
        for record in records[count_before:]:
            so_far = record.turn_cost_usd or 0.0
            updated = record.model_copy(update={"turn_cost_usd": so_far + cost_usd})
            self._conn.execute(
                "UPDATE audit_records SET record_json = ? WHERE proposal_id = ?",
                (updated.model_dump_json(), updated.proposal.proposal_id),
            )
        self._conn.commit()

    def cost_for_thread(self, thread_id: str) -> float:
        return sum(
            record.turn_cost_usd or 0.0
            for record in self.records()
            if record.proposal.thread_id == thread_id
        )

    def __len__(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM audit_records").fetchone()
        return int(row[0])
