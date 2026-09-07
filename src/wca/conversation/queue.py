"""One queue per thread, run one job at a time.

Meta does not promise webhook order. Without this, two messages in the
same conversation could both reach the gate for the same slot and both
pass. Different threads do not block each other.

v0 drains on demand, which keeps the tests deterministic. v1 runs a
worker per thread.
"""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Callable


class ThreadQueue:
    def __init__(self) -> None:
        self._queues: dict[str, deque[Callable[[], None]]] = defaultdict(deque)

    def submit(self, thread_id: str, job: Callable[[], None]) -> None:
        self._queues[thread_id].append(job)

    def pending(self, thread_id: str) -> int:
        return len(self._queues[thread_id])

    def drain(self) -> list[Exception]:
        """Run everything queued, thread by thread, in order.

        A job that raises does not stop the rest of its thread. The
        errors come back so the caller can decide.
        """
        errors: list[Exception] = []
        for thread_id in list(self._queues):
            queue = self._queues[thread_id]
            while queue:
                job = queue.popleft()
                try:
                    job()
                except Exception as err:  # noqa: BLE001
                    errors.append(err)
        return errors
