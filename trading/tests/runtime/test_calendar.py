"""Tests for NSETradingCalendar — weekend detection, holidays, next trading day, market hours."""

from __future__ import annotations

from datetime import date, datetime

from tradex_trading.runtime.calendar import NSETradingCalendar


def test_holiday_is_not_a_trading_day() -> None:
    """A weekday in the holiday set is not a trading day."""
    cal = NSETradingCalendar(holidays={date(2026, 8, 5)})  # Wednesday
    assert cal.is_trading_day(date(2026, 8, 5)) is False
    # Other weekdays still trade.
    assert cal.is_trading_day(date(2026, 8, 6)) is True
    # Default calendar has no holidays — the same Wednesday trades.
    assert NSETradingCalendar().is_trading_day(date(2026, 8, 5)) is True


def test_next_trading_day_skips_holiday() -> None:
    """Next trading day after a Wednesday holiday is Thursday, not skipped over."""
    cal = NSETradingCalendar(holidays={date(2026, 8, 6)})  # Thursday
    assert cal.next_trading_day(date(2026, 8, 5)) == date(2026, 8, 7)  # Friday


def test_next_trading_day_skips_holiday_then_weekend() -> None:
    """Holiday Friday → next trading day is Monday."""
    cal = NSETradingCalendar(holidays={date(2026, 8, 7)})  # Friday
    assert cal.next_trading_day(date(2026, 8, 6)) == date(2026, 8, 10)  # Monday


def test_is_market_open_closed_on_holiday() -> None:
    """Market is closed on a holiday even during market hours."""
    cal = NSETradingCalendar(holidays={date(2026, 8, 5)})
    assert cal.is_market_open(datetime(2026, 8, 5, 10, 0, 0)) is False
    assert cal.is_market_open(datetime(2026, 8, 6, 10, 0, 0)) is True


def test_trading_calendar_weekend_detection() -> None:
    """Saturday and Sunday are not trading days."""
    cal = NSETradingCalendar()
    # 2026-08-01 is Saturday, 2026-08-02 is Sunday
    saturday = date(2026, 8, 1)
    sunday = date(2026, 8, 2)
    assert cal.is_trading_day(saturday) is False
    assert cal.is_trading_day(sunday) is False
    # Monday should be a trading day
    monday = date(2026, 8, 3)
    assert cal.is_trading_day(monday) is True


def test_next_trading_day_skips_weekend() -> None:
    """Next trading day after Friday is Monday."""
    cal = NSETradingCalendar()
    friday = date(2026, 8, 7)  # Friday
    next_day = cal.next_trading_day(friday)
    assert next_day == date(2026, 8, 10)  # Monday
    assert next_day.weekday() == 0  # Monday


def test_is_market_open_within_hours() -> None:
    """Market is open within 09:15-15:30 on weekdays, closed outside."""
    cal = NSETradingCalendar()
    # Wednesday 10:00 — within hours
    within = datetime(2026, 8, 5, 10, 0, 0)
    assert cal.is_market_open(within) is True
    # Wednesday 08:00 — before market open
    before = datetime(2026, 8, 5, 8, 0, 0)
    assert cal.is_market_open(before) is False
    # Wednesday 16:00 — after market close
    after = datetime(2026, 8, 5, 16, 0, 0)
    assert cal.is_market_open(after) is False
    # Saturday 10:00 — weekend
    weekend = datetime(2026, 8, 1, 10, 0, 0)
    assert cal.is_market_open(weekend) is False
