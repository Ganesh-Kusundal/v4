"""Market data objects (FDS 05 §6.1/§6.2/§6.8, decisions D-4/D-5/D-11)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from tradex_domain.enums import ExchangeId, Timeframe
from tradex_domain.errors import CapabilityNotSupportedError
from tradex_domain.instruments import Instrument
from tradex_domain.serialization import Serializable
from tradex_domain.value_objects import Price, Quantity

#: Venue-wide depth constraint: every broker provides order-book depth for
#: NSE instruments only. BSE/MCX/NFO/… quotes stream but carry no depth feed.
_DEPTH_SUPPORTED_EXCHANGES: frozenset[str] = frozenset({ExchangeId.NSE})


def require_depth_supported(instrument: Instrument) -> None:
    """Raise ``CapabilityNotSupportedError`` unless *instrument* can carry a book.

    Market depth (order-book levels) is only provided on NSE across all
    brokers; requesting depth on any other exchange fails loudly instead of
    silently streaming an empty book.
    """
    if str(instrument.exchange) not in _DEPTH_SUPPORTED_EXCHANGES:
        raise CapabilityNotSupportedError(
            f"market depth is not supported on {instrument.exchange} (NSE only)"
        )


@dataclass(frozen=True, slots=True)
class OHLC(Serializable):
    open: Price
    high: Price
    low: Price
    close: Price

    @property
    def is_bullish(self) -> bool:
        return self.close.value > self.open.value

    @property
    def range(self) -> Price:
        """High minus low."""
        return Price(value=self.high.value - self.low.value)


@dataclass(frozen=True, slots=True)
class Depth(Serializable):
    instrument: Instrument
    bids: tuple[tuple[Price, Quantity], ...] = ()
    asks: tuple[tuple[Price, Quantity], ...] = ()
    timestamp: datetime | None = None

    def __post_init__(self) -> None:
        """Validate the book: bids must be price-descending, asks ascending.

        The best level is always ``bids[0]`` / ``asks[0]``, so producers must
        hand over an ordered book or the mismatch is loud at the boundary.
        """
        self._validate_side(self.bids, descending=True, side="bids")
        self._validate_side(self.asks, descending=False, side="asks")

    @staticmethod
    def _validate_side(
        levels: tuple[tuple[Price, Quantity], ...],
        *,
        descending: bool,
        side: str,
    ) -> None:
        previous: Decimal | None = None
        for level in levels:
            if (
                not isinstance(level, tuple)
                or len(level) != 2
                or not isinstance(level[0], Price)
                or not isinstance(level[1], Quantity)
            ):
                raise ValueError(f"Depth {side} levels must be (Price, Quantity) pairs")
            price = level[0].value
            if previous is not None:
                if descending and price > previous:
                    raise ValueError(
                        f"Depth {side} must be price-descending "
                        f"(got {price} after {previous})"
                    )
                if not descending and price < previous:
                    raise ValueError(
                        f"Depth {side} must be price-ascending "
                        f"(got {price} after {previous})"
                    )
            previous = price

    @property
    def best_bid(self) -> Price | None:
        """Highest bid price, or None if no bids."""
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Price | None:
        """Lowest ask price, or None if no asks."""
        return self.asks[0][0] if self.asks else None

    @property
    def mid_price(self) -> Price | None:
        """Mid-point between best bid and best ask."""
        if self.best_bid is None or self.best_ask is None:
            return None
        return Price(value=(self.best_bid.value + self.best_ask.value) / 2)

    @property
    def spread(self) -> Price | None:
        """Difference between best ask and best bid."""
        if self.best_bid is None or self.best_ask is None:
            return None
        return Price(value=self.best_ask.value - self.best_bid.value)


@dataclass(frozen=True, slots=True)
class Quote(Serializable):
    instrument: Instrument
    ltp: Price
    bid: Price | None = None
    ask: Price | None = None
    volume: Quantity | None = None
    open_interest: Quantity | None = None
    ohlc: OHLC | None = None
    depth: Depth | None = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    exchange: str = ""
    provider: str = ""
    metadata: dict[str, object] | None = None

    @property
    def spread(self) -> Price | None:
        """Ask minus bid, or None if either is missing."""
        if self.ask is None or self.bid is None:
            return None
        return Price(value=self.ask.value - self.bid.value)

    @property
    def mid_price(self) -> Price | None:
        """Average of ask and bid."""
        if self.ask is None or self.bid is None:
            return None
        return Price(value=(self.ask.value + self.bid.value) / 2)


@dataclass(frozen=True, slots=True)
class Candle(Serializable):
    instrument: Instrument
    timeframe: Timeframe
    ohlc: OHLC
    volume: Quantity
    timestamp: datetime

    @property
    def is_bullish(self) -> bool:
        return self.ohlc.close.value > self.ohlc.open.value

    @property
    def is_bearish(self) -> bool:
        return self.ohlc.close.value < self.ohlc.open.value


@dataclass(frozen=True, slots=True)
class HistoricalSeries(Serializable):
    """Canonical time-series return type (FDS 05 §6.2, D-5)."""

    instrument: Instrument
    timeframe: Timeframe
    candles: list[Candle]
    start: datetime
    end: datetime

    def resample(self, timeframe: Timeframe) -> HistoricalSeries:
        from tradex_domain.market import _bucketize

        buckets = _bucketize(self.candles, timeframe)
        candles = [b for b in buckets.values()]
        return HistoricalSeries(
            instrument=self.instrument,
            timeframe=timeframe,
            candles=candles,
            start=self.start,
            end=self.end,
        )

    def __len__(self) -> int:
        return len(self.candles)

    def __iter__(self) -> object:
        return iter(self.candles)

    def __getitem__(self, index: int | slice) -> Candle | HistoricalSeries:  # type: ignore[valid-type]
        if isinstance(index, slice):
            sliced_candles = cast("list[Candle]", self.candles[index])
            return HistoricalSeries(
                instrument=self.instrument,
                timeframe=self.timeframe,
                candles=sliced_candles,
                start=sliced_candles[0].timestamp if sliced_candles else self.start,
                end=sliced_candles[-1].timestamp if sliced_candles else self.end,
            )
        return self.candles[index]


def _aggregate(chunk: list[Candle]) -> Candle:
    from decimal import Decimal

    first = chunk[0]
    ohlc = first.ohlc
    return Candle(
        instrument=first.instrument,
        timeframe=first.timeframe,
        ohlc=type(ohlc)(
            open=ohlc.open,
            high=Price(value=max(c.ohlc.high.value for c in chunk)),
            low=Price(value=min(c.ohlc.low.value for c in chunk)),
            close=chunk[-1].ohlc.close,
        ),
        volume=Quantity(value=sum((c.volume.value for c in chunk), Decimal(0))),
        timestamp=chunk[-1].timestamp,
    )


def _bucketize(candles: list[Candle], timeframe: Timeframe) -> dict[int, Candle]:
    import calendar

    seconds = {
        Timeframe.M1: 60,
        Timeframe.M5: 300,
        Timeframe.M15: 900,
        Timeframe.M30: 1800,
        Timeframe.H1: 3600,
        Timeframe.D1: 86400,
        Timeframe.W1: 604800,
    }[timeframe]
    buckets: dict[int, list[Candle]] = {}
    for candle in candles:
        epoch = calendar.timegm(candle.timestamp.utctimetuple())
        key = (epoch // seconds) * seconds
        buckets.setdefault(key, []).append(candle)
    out: dict[int, Candle] = {}
    for key, chunk in buckets.items():
        agg = _aggregate(chunk)
        # Override timeframe to target (_aggregate uses the source candle's timeframe)
        out[key] = Candle(
            instrument=agg.instrument,
            timeframe=timeframe,
            ohlc=agg.ohlc,
            volume=agg.volume,
            timestamp=agg.timestamp,
        )
    return out


__all__ = [
    "Candle",
    "Depth",
    "HistoricalSeries",
    "OHLC",
    "Quote",
    "require_depth_supported",
]
