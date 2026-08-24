"""BarAggregator — builds interval bars from a quote stream.

One aggregation code path for every source: live broker quotes (MarketFeed)
and synthetic generator quotes (replay) both feed the same buckets, so a
forming bar means the same thing regardless of where the ticks came from.

The IST session grid rules here mirror the datalake's (09:15-15:30, calendar
day boundary): a bucket closes when the first quote past its end arrives, and
the day flat-closes at 15:30 so the last bar of a session is never left
hanging open until the next morning's tick.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from tradex_domain.enums import Timeframe

#: Minimum spacing between two forming-bar frames for one (bucket) — a burst
#: of quotes must not flood the socket. Closed-bar frames bypass the throttle.
FORMING_FRAME_MIN_INTERVAL = 1.0


@dataclass(frozen=True, slots=True)
class BarFrame:
    """One emitted bar: forming (closed=False) or final (closed=True)."""

    instrument: str  # instrument_id string, e.g. 'NSE:RELIANCE'
    timeframe: str  # canonical Timeframe value, e.g. '1m'
    time: int  # UTC epoch seconds of bucket start (chart convention)
    open: float
    high: float
    low: float
    close: float
    volume: float
    closed: bool


def bucket_start(ts: datetime, seconds: int) -> datetime:
    """Floor *ts* onto the fixed-interval grid."""
    epoch = int(ts.timestamp())
    return datetime.fromtimestamp(epoch - (epoch % seconds), tz=ts.tzinfo)


def _tf_seconds(timeframe: Timeframe) -> int:
    seconds = {
        Timeframe.M1: 60,
        Timeframe.M5: 300,
        Timeframe.M15: 900,
        Timeframe.M30: 1800,
        Timeframe.H1: 3600,
        Timeframe.D1: 86400,
    }.get(timeframe)
    if seconds is None:
        raise ValueError(f"bar aggregation unsupported for timeframe {timeframe}")
    return seconds


class _Bucket:
    """One accumulating bar."""

    __slots__ = ("start", "open", "high", "low", "close", "volume")

    def __init__(self, start: datetime, o: float, h: float, low: float, c: float, v: float) -> None:
        self.start = start
        self.open = o
        self.high = h
        self.low = low
        self.close = c
        self.volume = v

    def absorb(self, price: float, volume: float) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += volume


class BarAggregator:
    """Accumulates quotes into interval bars for ONE (instrument, timeframe).

    Not thread-safe by itself: callers on broker receive threads serialize
    through their own lock or the ThreadSafeReactiveBus before calling.
    """

    def __init__(
        self,
        instrument_id: str,
        timeframe: Timeframe,
        *,
        on_frame: Callable[[BarFrame], None],
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._instrument_id = instrument_id
        self._timeframe = timeframe
        self._seconds = _tf_seconds(timeframe)
        self._on_frame = on_frame
        self._now = now
        self._bucket: _Bucket | None = None
        # Negative infinity so the very first quote emits immediately no
        # matter what clock the caller injected (a test clock at zero would
        # otherwise swallow the first frame).
        self._last_forming_emit = float("-inf")

    # ------------------------------------------------------------------ input

    def on_quote(
        self,
        ts_ist: datetime,
        price: Decimal | float,
        volume: Decimal | float | None,
    ) -> None:
        """Feed one trade print (IST-naive timestamp like the datalake)."""
        p = float(price)
        v = float(volume) if volume is not None else 0.0
        start = bucket_start(ts_ist, self._seconds)

        if self._bucket is not None and start > self._bucket.start:
            self._emit(closed=True)

        if self._bucket is None or start > self._bucket.start:
            self._bucket = _Bucket(start, p, p, p, p, v)
        else:
            self._bucket.absorb(p, v)

        # Forming-bar updates are throttled; closed-bar emission above is not.
        now = self._now()
        if now - self._last_forming_emit >= FORMING_FRAME_MIN_INTERVAL:
            self._last_forming_emit = now
            self._emit(closed=False)

    def flush(self) -> None:
        """Close any open bucket (session end / teardown). Idempotent."""
        if self._bucket is not None:
            self._emit(closed=True)

    @property
    def last_closed_time(self) -> int | None:
        """UTC seconds of the last closed bucket start, or None."""
        return getattr(self, "_last_closed", None)

    # ----------------------------------------------------------------- output

    def _emit(self, *, closed: bool) -> None:
        assert self._bucket is not None
        b = self._bucket
        from zoneinfo import ZoneInfo

        ist = ZoneInfo("Asia/Kolkata")
        chart_time = int(b.start.replace(tzinfo=ist).timestamp())
        self._on_frame(
            BarFrame(
                instrument=self._instrument_id,
                timeframe=self._timeframe.value,
                time=chart_time,
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
                closed=closed,
            )
        )
        if closed:
            self._last_closed = chart_time
            self._bucket = None
            self._last_forming_emit = self._now()


__all__ = ["BarAggregator", "BarFrame", "bucket_start"]
