"""Which messages we have already handled.

Meta delivers a webhook at least once, which means sometimes more than
once. Without this, a retry books a second appointment.

Do the check where the queue is consumed, not at the edge. If it only
runs at the edge, several retries can arrive together and all pass before
any of them has been written down.

`SqliteDedupStore` alongside `DedupStore`: same contract, backed by the
`dedup_messages` table (see `wca.db`) instead of a Python `set()` that
resets with every restart -- the exact failure the PRD's platform-
constraints table warns about ("commit takes an idempotency key" only
helps if the dedup guarding it survives).
"""

from __future__ import annotations

import sqlite3


class DedupStore:
    def __init__(self) -> None:
        self._seen: set[str] = set()

    def seen(self, message_id: str) -> bool:
        return message_id in self._seen

    def remember(self, message_id: str) -> None:
        self._seen.add(message_id)

    def __len__(self) -> int:
        return len(self._seen)


class SqliteDedupStore:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def seen(self, message_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM dedup_messages WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None

    def remember(self, message_id: str) -> None:
        # A redelivered webhook calls remember() on something already
        # remembered -- that is the expected case (see the module
        # docstring), not an error, so the PRIMARY KEY collision it would
        # otherwise raise is swallowed here rather than bubbled up.
        try:
            self._conn.execute(
                "INSERT INTO dedup_messages (message_id) VALUES (?)", (message_id,)
            )
            self._conn.commit()
        except sqlite3.IntegrityError:
            self._conn.rollback()

    def __len__(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM dedup_messages").fetchone()
        return int(row[0])
