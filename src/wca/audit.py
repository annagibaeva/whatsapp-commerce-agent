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

    def __len__(self) -> int:
        return len(self._records)
