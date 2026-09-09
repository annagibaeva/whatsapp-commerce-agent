"""Identifiers and idempotency keys.

The idempotency key is a hash of the source, the kind of action, and the
payload. Committing the same booking twice produces the same key, so the
calendar can return the first booking instead of making a second one.
"""

import hashlib
import json
from datetime import datetime
from typing import Any


def idempotency_key(source: str, kind: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"s": source, "k": kind, "p": payload},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def hold_id(n: int) -> str:
    return f"hold_{n:04d}"


def proposal_id(thread_id: str, now: datetime, n: int) -> str:
    """`prop_<scope digest>_<counter>`.

    `n` alone is only unique within one turn -- `ToolContext._counter`
    starts fresh every turn (a `ToolContext` is rebuilt per inbound
    message), while `AuditLog` is process-wide and dedups `append` on
    this exact id. Without a turn-scoped prefix, the first proposal of
    every turn on every thread collided on the same id and every
    booking after the first in a process silently lost its audit
    record.

    `thread_id` plus `now` is unique per turn: the per-thread serial
    queue means two turns on one thread never share a `now`. Hashed
    (same pattern as `idempotency_key` above) rather than embedded
    directly -- thread ids are customer phone numbers and must not
    appear, even in part, in an id that ends up in an audit record or a
    log line.
    """
    scope = hashlib.sha256(f"{thread_id}|{now.isoformat()}".encode("utf-8")).hexdigest()[:12]
    return f"prop_{scope}_{n:04d}"
