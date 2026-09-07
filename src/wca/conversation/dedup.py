"""Which messages we have already handled.

Meta delivers a webhook at least once, which means sometimes more than
once. Without this, a retry books a second appointment.

Do the check where the queue is consumed, not at the edge. If it only
runs at the edge, several retries can arrive together and all pass before
any of them has been written down.
"""

from __future__ import annotations


class DedupStore:
    def __init__(self) -> None:
        self._seen: set[str] = set()

    def seen(self, message_id: str) -> bool:
        return message_id in self._seen

    def remember(self, message_id: str) -> None:
        self._seen.add(message_id)

    def __len__(self) -> int:
        return len(self._seen)
