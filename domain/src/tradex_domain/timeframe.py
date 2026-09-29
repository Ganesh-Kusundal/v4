"""Single-source timeframe registry (REF-02).

Previously every consumer re-declared its own mapping:

  domain/market.py:_bucketize  seconds dict
  brokers/dhan/_marketdata     interval_map + requested_timeframe
  brokers/upstox/_marketdata   _unit_interval + _target_timeframe
  trading/datalake/parallel_fetcher  _DHAN_INTRADAY_TIMEFRAMES

Ponytail: one dict of dicts, typed, stdlib only.  Dhan lacks M30 — its
interval is ``None`` so callers fail at the registry, not at the wire.

The bar *grid* lives here too (:func:`bucket_start`), for the same reason: three
sites bucket M1 into wider bars — the live aggregator, ``HistoricalSeries
.resample`` and the vectorised bulk provider — and when each of them floored to
the epoch on its own, the live feed and the chart ended up on different grids.
One definition, anchored at the session open, that all three call.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from tradex_domain.enums import Timeframe
from tradex_domain.market_calendar import MARKET_OPEN, to_ist_naive

# canon -> {dhan_interval, upstox_unit, upstox_interval, bucket_seconds, is_intraday}
_TIMEFRAMES: dict[str, dict[str, object]] = {
    "1m": {"dhan": "1", "upstox_unit": "minutes",
       "upstox_interval": "1", "seconds": 60, "intraday": True},
    "5m": {"dhan": "5", "upstox_unit": "minutes",
       "upstox_interval": "5", "seconds": 300, "intraday": True},
    "15m": {"dhan": "15", "upstox_unit": "minutes",
        "upstox_interval": "15", "seconds": 900, "intraday": True},
    "30m": {"dhan": None, "upstox_unit": "minutes",
        "upstox_interval": "30", "seconds": 1800, "intraday": True},
    "1h": {"dhan": "60", "upstox_unit": "hours",
       "upstox_interval": "1", "seconds": 3600, "intraday": True},
    "1d": {"dhan": None, "upstox_unit": "days",
       "upstox_interval": "1", "seconds": 86400, "intraday": False},
    "1w": {"dhan": None, "upstox_unit": "weeks",
       "upstox_interval": "1", "seconds": 604800, "intraday": False},
}

_TF_BY_VALUE: dict[str, Timeframe] = {
    "1m": Timeframe.M1, "5m": Timeframe.M5, "15m": Timeframe.M15,
    "30m": Timeframe.M30, "1h": Timeframe.H1, "1d": Timeframe.D1, "1w": Timeframe.W1,
}

def normalize(value: Timeframe | str) -> tuple[str, Timeframe]:
    """Return (canonical_str, Timeframe) for ``str | Timeframe``."""
    raw = value.value if isinstance(value, Timeframe) else str(value)
    tf = _TF_BY_VALUE.get(raw)
    if tf is None:
        raise ValueError(f"unsupported timeframe: {value!r}")
    return raw, tf


def bucket_seconds(tf: Timeframe) -> int:
    """Seconds per bar for resample/bucketize."""
    raw, _ = normalize(tf)
    return _TIMEFRAMES[raw]["seconds"]  # type: ignore[return-value]


#: The grid is IST wall clock, so wall stamps are counted from this epoch and
#: no timezone arithmetic appears in the rule at all. IST has no DST, so a
#: naive wall delta is the elapsed time.
_WALL_EPOCH = datetime(1970, 1, 1)

#: Seconds from IST midnight to the session open, read from the one copy of
#: 09:15 in the calendar rather than restated here.
_SESSION_OPEN_WALL = (
    MARKET_OPEN.hour * 3600 + MARKET_OPEN.minute * 60 + MARKET_OPEN.second
)

_DAY_WALL = bucket_seconds(Timeframe.D1)
_WEEK_WALL = bucket_seconds(Timeframe.W1)
#: 1970-01-01 was a Thursday, which is why a 604800s floor lands mid-week.
_EPOCH_WEEKDAY = 3


def to_wall_seconds(ts: datetime) -> int:
    """Seconds from 1970-01-01 to *ts*, read as IST wall clock.

    Naive input passes through (the datalake contract stores tz-naive IST);
    aware input is converted to IST first, matching :func:`to_ist_naive`.
    """
    return int((to_ist_naive(ts) - _WALL_EPOCH).total_seconds())


def from_wall_seconds(seconds: int) -> datetime:
    """Inverse of :func:`to_wall_seconds` — a naive IST wall clock stamp."""
    return _WALL_EPOCH + timedelta(seconds=seconds)


def bucket_wall_seconds(wall: int, seconds: int) -> int:
    """Session-anchored bucket start of wall stamp *wall*, in wall seconds.

    The whole grid, in one function, because three sites bucket and a second
    copy is a second grid:

    * **intraday** — the 09:15 session open, so the bar holding *wall* is the
      open plus the largest whole multiple of *seconds* that does not pass it.
      A fixed-length floor is skewed twice over: by IST's 19,800s offset, and by
      33,300s because the open is not a whole number of every bar width from
      midnight. 33,300 divides by 60, 300 and 900 but not 1800 or 3600, so the
      common 1m/5m/15m widths were accidentally correct and only 30m and hourly
      were wrong — an hourly bar stamped 08:30 holding fifteen minutes of the
      session's first hour, and a 30m backtest on a different grid than the 30m
      chart that would trade it.
    * **daily** — IST midnight, not the 00:00 UTC epoch day, which falls at
      05:30 IST, in the middle of the trading day.
    * **weekly** — IST Monday 00:00. 604800 is divisible by seven days, but the
      epoch origin is a Thursday, so a fixed-length floor lands mid-week.

    Floor, not truncate: a tick a fraction of a second before a seam belongs to
    the bar before it, and the floor must be non-decreasing in *wall* for the
    aggregator, which decides "close the bar" by comparing bucket starts.
    """
    if seconds == _DAY_WALL:
        return wall - (wall % _DAY_WALL)
    if seconds == _WEEK_WALL:
        midnight = wall - (wall % _DAY_WALL)
        weekday = (midnight // _DAY_WALL + _EPOCH_WEEKDAY) % 7
        return midnight - weekday * _DAY_WALL
    return _SESSION_OPEN_WALL + (
        (wall - _SESSION_OPEN_WALL) // seconds
    ) * seconds


def bucket_start(ts: datetime, seconds: int) -> datetime:
    """Start of the bar of width *seconds* that holds *ts*, as naive IST.

    The datetime face of :func:`bucket_wall_seconds`, and the only bar grid in
    the platform. Timestamps are IST wall clock: the datalake stores them
    tz-naive, brokers hand them over converted, and the chart renders in
    Asia/Kolkata. A naive ``timestamp()`` follows the process timezone instead
    and shifts every multi-hour bar off the history series on a non-IST host.
    """
    return from_wall_seconds(bucket_wall_seconds(to_wall_seconds(ts), seconds))


def dhan_interval(value: Timeframe | str) -> str | None:
    """Dhan ``interval`` string (``None`` if unsupported, e.g. M30)."""
    raw, _ = normalize(value)
    return _TIMEFRAMES[raw]["dhan"]  # type: ignore[return-value]


def upstox_unit_interval(value: Timeframe | str) -> tuple[str, str]:
    """Upstox ``(unit, interval)`` pair."""
    raw, _ = normalize(value)
    row = _TIMEFRAMES[raw]
    return str(row["upstox_unit"]), str(row["upstox_interval"])


def to_timeframe(value: Timeframe | str) -> Timeframe:
    """Canonical ``Timeframe`` for any accepted value."""
    _, tf = normalize(value)
    return tf


__all__ = [
    "bucket_seconds",
    "bucket_start",
    "bucket_wall_seconds",
    "dhan_interval",
    "from_wall_seconds",
    "normalize",
    "to_timeframe",
    "to_wall_seconds",
    "upstox_unit_interval",
]
