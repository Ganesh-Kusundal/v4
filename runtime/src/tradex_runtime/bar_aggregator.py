"""BarAggregator — builds interval bars from a quote stream.

One aggregation code path for every source: live broker quotes (MarketFeed)
and synthetic generator quotes (replay) both feed the same buckets, so a
forming bar means the same thing regardless of where the ticks came from.

The IST session grid rules live in ``tradex_domain.timeframe`` — the one
definition, anchored at the 09:15 session open — because two history sites
bucket the same M1 bars into wider ones and a second copy here is a second
grid. :func:`bucket_start` is re-exported from there, not redefined, so
``domain <- brokers <- trading`` holds and the live feed cannot disagree with
the chart.

The session close is not the aggregator's to decide — :meth:`BarAggregator.flush`
closes whatever is open and it is the caller's to call at 15:30 (or on teardown
or replay stop), so nothing here reads a wall clock and no test has to inject
one to reach the close deterministically.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from tradex_domain.enums import Timeframe
from tradex_domain.timeframe import bucket_seconds, bucket_start

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
    source: str = "live"  # 'live' broker quotes vs 'sim' replay ticks
    run_id: str | None = None
    provisional: bool = False
    feed_id: str | None = None
    connection_generation: int | None = None
    quality_flags: tuple[str, ...] = ()


def _tf_seconds(timeframe: Timeframe) -> int:
    """Bucket width in seconds for *timeframe*, from the canonical registry."""
    try:
        return bucket_seconds(timeframe)
    except KeyError:
        raise ValueError(f"bar aggregation unsupported for timeframe {timeframe}") from None

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
        run_id: str | None = None,
        provisional: bool = False,
        feed_id: str | None = None,
        connection_generation: int | None = None,
        quality_flags: tuple[str, ...] = (),
    ) -> None:
        self._instrument_id = instrument_id
        self._timeframe = timeframe
        self._seconds = _tf_seconds(timeframe)
        self._on_frame = on_frame
        self._now = now
        self._run_id = run_id
        self._feed_id = feed_id
        self._connection_generation = connection_generation
        self._quality_flags = tuple(quality_flags)
        self._bucket: _Bucket | None = None
        self._source = "live"
        self._disposed = False
        self._provisional_default = bool(provisional)
        self._provisional = bool(provisional)
        self._has_streamed_bucket = False
        self._seeded_closed_start: datetime | None = None
        # Negative infinity so the very first quote emits immediately no
        # matter what clock the caller injected (a test clock at zero would
        # otherwise swallow the first frame).
        self._last_forming_emit = float("-inf")
        self._last_closed: int | None = None

    # ------------------------------------------------------------------ input

    def seed_last_closed(self, bar: BarFrame) -> None:
        self._validate_seed(bar)
        self._seeded_closed_start = self._seed_start(bar.time)
        self._last_closed = self._chart_time(self._seeded_closed_start)

    def seed_current_bucket(self, bar: BarFrame, *, provisional: bool | None = None) -> None:
        self._validate_seed(bar)
        if bar.closed:
            raise ValueError("current bucket seed must be forming")
        if self._bucket is not None:
            raise ValueError("cannot seed a bucket after aggregation starts")
        start = self._seed_start(bar.time)
        if self._seeded_closed_start is not None and start <= self._seeded_closed_start:
            raise ValueError("current bucket must follow the last closed bucket")
        self._bucket = _Bucket(start, bar.open, bar.high, bar.low, bar.close, bar.volume)
        self._provisional = self._provisional_default if provisional is None else bool(provisional)
        self._has_streamed_bucket = True
        self._last_forming_emit = float("-inf")

    def _validate_seed(self, bar: BarFrame) -> None:
        if bar.instrument != self._instrument_id or bar.timeframe != self._timeframe.value:
            raise ValueError("bar seed does not match aggregator")
        values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
        if any(not math.isfinite(float(value)) for value in values):
            raise ValueError("bar seed values must be finite")

    def _seed_start(self, chart_time: int) -> datetime:
        from zoneinfo import ZoneInfo

        aware = datetime.fromtimestamp(int(chart_time), tz=ZoneInfo("Asia/Kolkata"))
        return bucket_start(aware.replace(tzinfo=None), self._seconds)

    def _chart_time(self, start: datetime) -> int:
        from zoneinfo import ZoneInfo

        return int(start.replace(tzinfo=ZoneInfo("Asia/Kolkata")).timestamp())

    def on_quote(
        self,
        ts_ist: datetime,
        price: Decimal | float,
        volume: Decimal | float | None,
        source: str = "live",
    ) -> None:
        """Feed one trade print (IST-naive timestamp like the datalake).

        ``source`` rides on every frame this quote emits so a consumer can
        tell broker ticks from synthetic replay ticks."""
        if self._disposed:
            return
        p = float(price)
        v = float(volume) if volume is not None else 0.0
        start = bucket_start(ts_ist, self._seconds)

        starting_new = False
        if self._bucket is not None:
            if start < self._bucket.start:
                return
            self._source = source
            if start > self._bucket.start:
                self._emit(closed=True)
                starting_new = True

        if self._bucket is None:
            if self._seeded_closed_start is not None and start <= self._seeded_closed_start:
                return
            starting_new = True

        if starting_new:
            self._source = source
            self._provisional = (
                self._provisional_default if not self._has_streamed_bucket else False
            )
            self._bucket = _Bucket(start, p, p, p, p, v)
        else:
            assert self._bucket is not None
            self._bucket.absorb(p, v)
        self._has_streamed_bucket = True

        # Forming-bar updates are throttled for live broker quotes; simulation / replay
        # ticks must emit every intra-candle update so candle formation is visible on the chart.
        now = self._now()
        if (
            source == "sim"
            or starting_new
            or now - self._last_forming_emit >= FORMING_FRAME_MIN_INTERVAL
        ):
            self._last_forming_emit = now
            self._emit(closed=False)

    def flush(self) -> None:
        """Close any open bucket (session end / teardown). Idempotent."""
        if self._bucket is not None and not self._disposed:
            self._emit(closed=True)

    def dispose(self, *, flush: bool = True) -> None:
        if self._disposed:
            return
        try:
            if flush:
                self.flush()
        finally:
            self._disposed = True
            self._bucket = None

    @property
    def last_closed_time(self) -> int | None:
        """UTC seconds of the last closed bucket start, or None."""
        return getattr(self, "_last_closed", None)

    # ----------------------------------------------------------------- output

    def _emit(self, *, closed: bool) -> None:
        assert self._bucket is not None
        b = self._bucket
        chart_time = self._chart_time(b.start)
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
                source=self._source,
                run_id=self._run_id,
                provisional=self._provisional,
                feed_id=self._feed_id,
                connection_generation=self._connection_generation,
                quality_flags=self._quality_flags,
            )
        )
        if closed:
            self._last_closed = chart_time
            self._bucket = None
            self._last_forming_emit = self._now()


__all__ = ["BarAggregator", "BarFrame", "bucket_start"]
