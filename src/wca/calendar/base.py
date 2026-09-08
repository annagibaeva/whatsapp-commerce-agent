"""What any calendar has to do.

v0 ships the mock. v1 puts a real calendar behind the same protocol.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol


class CalendarPort(Protocol):
    def availability(self, now: datetime) -> tuple[str, ...]: ...

    def hold(self, slot_id: str, thread_id: str, now: datetime) -> Any: ...

    def commit(
        self, hold_id: str, idempotency_key: str, now: datetime, service_id: str | None = None
    ) -> Any: ...

    def release(self, hold_id: str, reason: str, now: datetime) -> None: ...

    def expire_due(self, now: datetime) -> list[Any]: ...

    def view(self, slot_id: str, now: datetime) -> dict[str, Any]: ...

    def mark_reminded(self, booking_id: str, now: datetime) -> Any: ...

    def due_reminders(self, within_hours: float, now: datetime) -> list[Any]: ...
