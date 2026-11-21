"""The session-close flush: open bars close at 15:30 IST, once per day (A5)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tradex_domain.market_calendar import IST
from tradex_runtime.session_close import SessionCloseFlush


class _Clock:
    """A settable clock; the class under test must never read the wall."""

    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def set(self, at: datetime) -> None:
        self.at = at


def _at(hour: int, minute: int, *, day: int = 24) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=IST)


def test_not_due_before_the_close() -> None:
    flushed: list[int] = []
    clock = _Clock(_at(15, 29))
    closer = SessionCloseFlush(lambda: flushed.append(1), now=clock)

    assert closer.flush_if_due() is False
    assert flushed == []


def test_due_at_the_close_and_only_once_that_day() -> None:
    flushed: list[int] = []
    clock = _Clock(_at(15, 30))
    closer = SessionCloseFlush(lambda: flushed.append(1), now=clock)

    assert closer.flush_if_due() is True
    assert len(flushed) == 1

    # Polls arrive every 30s; a second close the same day is not a new close.
    clock.set(_at(15, 31))
    assert closer.flush_if_due() is False
    assert len(flushed) == 1


def test_rearms_on_the_next_trading_day() -> None:
    flushed: list[int] = []
    clock = _Clock(_at(15, 30, day=24))
    closer = SessionCloseFlush(lambda: flushed.append(1), now=clock)

    assert closer.flush_if_due() is True
    clock.set(_at(15, 30, day=25))
    assert closer.flush_if_due() is True
    assert len(flushed) == 2


def test_a_utc_clock_is_read_as_ist() -> None:
    # 10:00 UTC is 15:30 IST: the close is IST wall clock, so a clock left in
    # UTC must not be compared to a naive 15:30 (which would fire 5h30m late).
    flushed: list[int] = []
    clock = _Clock(datetime(2026, 9, 24, 10, 0, tzinfo=UTC))
    closer = SessionCloseFlush(lambda: flushed.append(1), now=clock)

    assert closer.flush_if_due() is True
    assert len(flushed) == 1


def test_a_failed_flush_is_retried_rather_than_skipped_for_the_day() -> None:
    calls: list[int] = []

    def flaky() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("publish failed")

    clock = _Clock(_at(15, 30))
    closer = SessionCloseFlush(flaky, now=clock)

    with pytest.raises(RuntimeError):
        closer.flush_if_due()
    # The date is only recorded after a successful close, so the next poll
    # retries instead of leaving the day unclosed.
    assert closer.flush_if_due() is True
    assert len(calls) == 2
    assert closer.flush_if_due() is False
