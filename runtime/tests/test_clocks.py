"""Tests for deterministic clock abstractions."""

from datetime import datetime, timedelta, UTC

from tradex_runtime.clocks import LiveClock, SimClock, ReplayClock


def test_sim_clock_deterministic():
    start = datetime(2024, 6, 1, 9, 15, tzinfo=UTC)
    c = SimClock(start)
    assert c.now() == start
    c.advance_by(timedelta(minutes=1))
    assert c.now() == start + timedelta(minutes=1)
    c.set(datetime(2024, 6, 1, 15, 30, tzinfo=UTC))
    assert c.now() == datetime(2024, 6, 1, 15, 30, tzinfo=UTC)
    assert c.is_live() is False


def test_sim_clock_advance_to():
    start = datetime(2024, 6, 1, 9, 15, tzinfo=UTC)
    c = SimClock(start)
    later = datetime(2024, 6, 1, 10, 0, tzinfo=UTC)
    c.advance(later)
    assert c.now() == later
    # advance to earlier time is no-op
    c.advance(start)
    assert c.now() == later


def test_live_clock_is_live():
    c = LiveClock()
    assert c.is_live() is True
    # now() returns a tz-aware datetime
    assert c.now().tzinfo is not None


def test_replay_clock():
    ts1 = datetime(2024, 6, 1, 9, 15, tzinfo=UTC)
    ts2 = datetime(2024, 6, 1, 9, 16, tzinfo=UTC)
    c = ReplayClock([ts1, ts2])
    assert c.now() == ts1
    c.advance(ts2)
    assert c.now() == ts2
    assert c.is_live() is False


def test_replay_clock_feed():
    c = ReplayClock()
    ts = datetime(2024, 6, 1, 9, 15, tzinfo=UTC)
    c.feed(ts)
    c.advance(ts)
    assert c.now() == ts
