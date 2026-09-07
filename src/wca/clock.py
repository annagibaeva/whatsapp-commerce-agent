"""Time handling.

Library code never calls `datetime.now()`. Every function that needs the
time takes a `now` parameter instead. That is the only reason the 24-hour
window tests can run in under a second.
"""

from datetime import datetime, timedelta, timezone

UTC = timezone.utc


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """Build a timezone-aware UTC datetime."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def hours_between(earlier: datetime, later: datetime) -> float:
    """Hours from `earlier` to `later`. Returns 0.0 if `later` is first."""
    return max(0.0, (later - earlier).total_seconds() / 3600.0)


class SimulatedClock:
    """A clock you move by hand.

    Tests need to sit at hour 23 of a 24-hour window. Waiting a day is not
    an option, so the tests move this instead.
    """

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("SimulatedClock needs a timezone-aware start")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, *, hours: float = 0, minutes: float = 0) -> datetime:
        step = timedelta(hours=hours, minutes=minutes)
        if step.total_seconds() < 0:
            raise ValueError("a clock does not run backwards")
        self._now = self._now + step
        return self._now
