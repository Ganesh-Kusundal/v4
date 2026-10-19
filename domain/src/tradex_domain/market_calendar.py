"""Single-source market session calendar.

Owns the only copy of ``09:15`` / ``15:30`` and MCX ``09:00``/``23:30``
wall-clock bounds.  Previously this literal was triplicated in
``trading/runtime/calendar.py``, ``trading/datalake/parquet_storage.py`` and
``brokers/dhan/_marketdata.py`` — a textbook shotgun-surgery literal.

Ponytail: one file, stdlib only, no broker/trading import.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

# NSE/BSE cash session (IST wall time)
MARKET_OPEN: time = time(9, 15)
MARKET_CLOSE: time = time(15, 30)

# Canonical Indian market timezone (moved here from ``tradex_domain.constants``
# so the calendar owns the one copy its session bounds are read in).
#
# A fixed +05:30 offset, deliberately not ``ZoneInfo("Asia/Kolkata")``. The two
# agree on every instant after 1945 (India has observed no DST since), but a
# ``ZoneInfo`` answers ``utcoffset(None)`` with ``None`` because its offset is
# date-dependent, and callers that ask the zone for its offset without a
# datetime get nothing back. A fixed offset answers, which is what the session
# grid arithmetic relies on.
IST = timezone(timedelta(hours=5, minutes=30))


def to_ist_naive(dt: datetime) -> datetime:
    """Normalize a timestamp to tz-naive IST (the datalake storage contract).

    Naive input passes through untouched (assumed already IST); aware input is
    converted from its source zone to IST and stripped of tzinfo.
    """
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(IST).replace(tzinfo=None)

MARKET_OPEN_STR: str = "09:15:00"
MARKET_CLOSE_STR: str = "15:30:00"

# Per-exchange Dhan intraday session bounds (v2 parity).  Unknown exchange
# falls back to the cash defaults above.
DHAN_SEGMENT_OPEN: dict[str, str] = {"MCX_COMM": "09:00:00", "NSE_COMM": "09:00:00"}
DHAN_SEGMENT_CLOSE: dict[str, str] = {"MCX_COMM": "23:30:00", "NSE_COMM": "23:30:00"}

#: NSE cash-market trading holidays for 2026 (weekday sessions with no
#: trading across the exchange).  Sources: the official NSE 2026 holiday
#: circular (Maharashtra Day, Ganesh Chaturthi, Gandhi Jayanti, Dussehra)
#: plus empirical datalake confirmations (Bakri Id, Muharram).  Extend as
#: new closures are confirmed — do not guess.
NSE_HOLIDAYS_2026: frozenset = frozenset({
    date(2026, 5, 1),
    date(2026, 5, 28),
    date(2026, 6, 26),
    date(2026, 9, 14),
    date(2026, 10, 2),
    date(2026, 10, 20),
})


class NSETradingCalendar:
    """NSE/BSE trading calendar utilities.

    Moved here from the deleted ``trading_calendar`` module so the session
    bounds and the calendar that reads them share one owner.

    Standard market hours: 09:15 to 15:30 IST, Monday to Friday. Exchange
    holidays are passed as a set of ``date`` values (e.g. from an annual
    NSE holiday list); a holiday is not a trading day even on a weekday,
    so ``is_trading_day``/``next_trading_day``/``is_market_open`` all
    respect it.
    """

    # Standard market hours (IST) — single-sourced from this module.
    _MARKET_OPEN = MARKET_OPEN
    _MARKET_CLOSE = MARKET_CLOSE

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
        """Check if the given date is a trading day (weekday, not a holiday)."""
        # Monday=0, Sunday=6
        return dt.weekday() < 5 and dt not in self._holidays

    def next_trading_day(self, dt: date) -> date:
        """Get the next trading day after the given date."""
        next_day = dt + timedelta(days=1)
        while not self.is_trading_day(next_day):
            next_day += timedelta(days=1)
        return next_day

    def market_open_time(self) -> time:
        """Get the market open time (09:15 IST)."""
        return self._MARKET_OPEN

    def market_close_time(self) -> time:
        """Get the market close time (15:30 IST)."""
        return self._MARKET_CLOSE

    def is_market_open(self, dt: datetime | None = None) -> bool:
        """Check if the market is currently open (weekday + 09:15-15:30)."""
        if dt is None:
            dt = datetime.now()

        if not self.is_trading_day(dt.date()):
            return False

        current_time = dt.time()
        return self._MARKET_OPEN <= current_time <= self._MARKET_CLOSE


__all__ = [
    "DHAN_SEGMENT_CLOSE",
    "DHAN_SEGMENT_OPEN",
    "IST",
    "MARKET_CLOSE",
    "MARKET_CLOSE_STR",
    "MARKET_OPEN",
    "MARKET_OPEN_STR",
    "NSETradingCalendar",
    "NSE_HOLIDAYS_2026",
    "to_ist_naive",
]
