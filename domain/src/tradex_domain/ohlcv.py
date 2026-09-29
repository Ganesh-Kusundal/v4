"""Single OHLCV aggregation path."""

from __future__ import annotations

from decimal import Decimal

from tradex_domain.market import Candle
from tradex_domain.value_objects import Price, Quantity


def aggregate_candles(candles: list[Candle]) -> Candle:
    """Aggregate *candles* into one candle.

    open=first, high=max, low=min, close=last, volume=sum.
    """
    if not candles:
        raise ValueError("cannot aggregate an empty candle list")
    first = candles[0]
    return Candle(
        instrument=first.instrument,
        timeframe=first.timeframe,
        ohlc=type(first.ohlc)(
            open=first.ohlc.open,
            high=Price(value=max(c.ohlc.high.value for c in candles)),
            low=Price(value=min(c.ohlc.low.value for c in candles)),
            close=candles[-1].ohlc.close,
        ),
        volume=Quantity(value=sum((c.volume.value for c in candles), Decimal(0))),
        timestamp=candles[-1].timestamp,
    )


__all__ = ["aggregate_candles"]
