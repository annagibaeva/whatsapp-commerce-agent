from datetime import datetime, timedelta

import pytest

from wca.clock import UTC, SimulatedClock, hours_between, utc


def test_utc_helper_is_timezone_aware():
    when = utc(2026, 8, 21, 14, 30)
    assert when == datetime(2026, 8, 21, 14, 30, tzinfo=UTC)
    assert when.utcoffset() == timedelta(0)


def test_hours_between_is_positive_and_clamped():
    a = utc(2026, 8, 21, 10)
    b = utc(2026, 8, 21, 13, 30)
    assert hours_between(a, b) == 3.5
    assert hours_between(b, a) == 0.0


def test_simulated_clock_starts_where_told():
    clock = SimulatedClock(utc(2026, 8, 21, 9))
    assert clock.now() == utc(2026, 8, 21, 9)


def test_simulated_clock_advances():
    clock = SimulatedClock(utc(2026, 8, 21, 9))
    clock.advance(hours=23)
    assert clock.now() == utc(2026, 8, 22, 8)
    clock.advance(minutes=90)
    assert clock.now() == utc(2026, 8, 22, 9, 30)


def test_simulated_clock_never_goes_backwards():
    clock = SimulatedClock(utc(2026, 8, 21, 9))
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(hours=-1)
