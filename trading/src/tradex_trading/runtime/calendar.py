"""NSE/BSE trading calendar utilities.

Provides trading day detection and market hours for Indian exchanges.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta


class NSETradingCalendar:
    """NSE/BSE trading calendar utilities.

    Standard market hours: 09:15 to 15:30 IST, Monday to Friday.
    Does not account for exchange-specific holidays (yet).
    """

    # Standard market hours (IST)
    _MARKET_OPEN = time(9, 15)
    _MARKET_CLOSE = time(15, 30)

    def is_trading_day(self, dt: date) -> bool:
        """Check if the given date is a trading day (Mon-Fri).

        Parameters
        ----------
        dt : date
            The date to check.

        Returns
        -------
        bool
            True if it's a trading day (weekday).
        """
        # Monday=0, Sunday=6
        return dt.weekday() < 5

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
