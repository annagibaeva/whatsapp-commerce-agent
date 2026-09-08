"""One queue per thread, run one job at a time.

Meta does not promise webhook order. Without this, two messages in the
same conversation could both reach the gate for the same slot and both
pass. Different threads do not block each other.

v0 drains on demand, which keeps the tests deterministic. v1 runs a
worker per thread.

Ordering and exclusion are different guarantees. A deque gives ordering:
jobs for one thread come out in the order they went in. It does not stop
two callers from both popping from that deque at the same time and
running two jobs from the same thread concurrently. The per-thread lock
below is what gives exclusion: only one caller at a time may be running
jobs for a given thread_id. Two callers working on different thread_ids
still run fully in parallel, because they take different locks.
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from typing import Callable


class ThreadQueue:
    def __init__(self) -> None:
        self._queues: dict[str, deque[Callable[[], None]]] = defaultdict(deque)
        # One lock per thread_id, created lazily. Guards against two
        # callers draining the same thread's queue at once.
        self._locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
        # Guards creation of entries in _queues / _locks themselves, since
        # defaultdict's __getitem__ is not safe against two threads adding
        # the same missing key at once.
        self._registry_lock = threading.Lock()

    def _lock_for(self, thread_id: str) -> threading.Lock:
        with self._registry_lock:
            return self._locks[thread_id]

    def submit(self, thread_id: str, job: Callable[[], None]) -> None:
        with self._registry_lock:
            self._queues[thread_id].append(job)

    def pending(self, thread_id: str) -> int:
        with self._registry_lock:
            return len(self._queues[thread_id])

    def drain_thread(self, thread_id: str) -> list[Exception]:
        """Run one thread's queued jobs, one at a time, in order.

        Held under that thread's own lock, so two callers draining the
        same thread_id serialize here instead of interleaving. A caller
        draining a different thread_id is not blocked by this at all.

        A job that raises does not stop the rest of its thread. The
        errors come back so the caller can decide.
        """
        errors: list[Exception] = []
        lock = self._lock_for(thread_id)
        with self._registry_lock:
            queue = self._queues[thread_id]
        # Holding this thread's lock for the whole drain is what makes it
        # exclusive: a second caller calling drain_thread(thread_id) blocks
        # on the same lock until this one finishes. submit() only appends
        # to the deque and does not need this lock, so new jobs can still
        # arrive mid-drain and will be picked up below.
        with lock:
            while queue:
                job = queue.popleft()
                try:
                    job()
                except Exception as err:  # noqa: BLE001
                    errors.append(err)
        return errors

    def drain(self) -> list[Exception]:
        """Run everything queued, thread by thread, in order.

        Kept for existing offline callers that want to drain every
        thread from one place. Implemented on top of drain_thread, so it
        gets the same per-thread exclusion.
        """
        errors: list[Exception] = []
        with self._registry_lock:
            thread_ids = list(self._queues)
        for thread_id in thread_ids:
            errors.extend(self.drain_thread(thread_id))
        return errors
