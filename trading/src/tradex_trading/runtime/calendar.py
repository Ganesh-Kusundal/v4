"""NSE/BSE trading calendar utilities.

Provides trading day detection and market hours for Indian exchanges.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta


class NSETradingCalendar:
    """NSE/BSE trading calendar utilities.

    Standard market hours: 09:15 to 15:30 IST, Monday to Friday. Exchange
    holidays are passed as a set of ``date`` values (e.g. from an annual NSE
    holiday list); a holiday is not a trading day even on a weekday, so
    ``is_trading_day``/``next_trading_day``/``is_market_open`` all respect it
    (ARCHITECTURE.md:757 — previously "does not yet account for exchange-
    specific holidays").
    """

    # Standard market hours (IST)
    _MARKET_OPEN = time(9, 15)
    _MARKET_CLOSE = time(15, 30)

    def __init__(self, holidays: set[date] | None = None) -> None:
        """Create a calendar.

        Parameters
        ----------
        holidays : set[date] | None
            Exchange holiday dates (weekdays on which the market is closed).
            Defaults to no holidays — weekday-only behavior.
        """
        self._holidays = frozenset(holidays) if holidays else frozenset()

    def is_trading_day(self, dt: date) -> bool:
        """Check if the given date is a trading day.

        A date is a trading day iff it is a weekday (Mon-Fri) and not an
        exchange holiday.

        Parameters
        ----------
        dt : date
            The date to check.

        Returns
        -------
        bool
            True if it's a trading day.
        """
        # Monday=0, Sunday=6
        return dt.weekday() < 5 and dt not in self._holidays

    def next_trading_day(self, dt: date) -> date:
        """Get the next trading day after the given date.

        Parameters
        ----------
        dt : date
            The starting date.

        Returns
        -------
        date
            The next trading day.
        """
        next_day = dt + timedelta(days=1)
        while not self.is_trading_day(next_day):
            next_day += timedelta(days=1)
        return next_day

    def market_open_time(self) -> time:
        """Get the market open time (09:15 IST).

        Returns
        -------
        time
            Market open time.
        """
        return self._MARKET_OPEN

    def market_close_time(self) -> time:
        """Get the market close time (15:30 IST).

        Returns
        -------
        time
            Market close time.
        """
        return self._MARKET_CLOSE

    def is_market_open(self, dt: datetime | None = None) -> bool:
        """Check if the market is currently open.

        Parameters
        ----------
        dt : datetime | None
            The datetime to check. If None, uses current time.

        Returns
        -------
        bool
            True if market is open (weekday + within market hours).
        """
        if dt is None:
            dt = datetime.now()

        if not self.is_trading_day(dt.date()):
            return False

        current_time = dt.time()
        return self._MARKET_OPEN <= current_time <= self._MARKET_CLOSE


__all__ = ["NSETradingCalendar"]
