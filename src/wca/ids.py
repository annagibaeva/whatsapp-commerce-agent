"""Identifiers and idempotency keys.

The idempotency key is a hash of the source, the kind of action, and the
payload. Committing the same booking twice produces the same key, so the
calendar can return the first booking instead of making a second one.
"""

import hashlib
import json
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


def proposal_id(n: int) -> str:
    return f"prop_{n:04d}"
