"""Market builders — DataFrame to Candle conversion.

Extracted from brokers.common.market_builders so both market_data and brokers
can use it without a layering violation.
"""

from __future__ import annotations

from typing import Any

from tradex_domain.enums import Timeframe
from tradex_domain.instruments import Instrument
from tradex_domain.market import OHLC, Candle
from tradex_domain.value_objects import Quantity

from tradex_shared.rate_limiting import as_decimal, as_price


def make_candle(
    instrument: Instrument,
    timeframe: Timeframe,
    *,
    open: Any,
    high: Any,
    low: Any,
    close: Any,
    volume: Any = 0,
    timestamp: Any,
) -> Candle:
    """One owner for as_price/as_decimal + OHLC wiring."""
    return Candle(
        instrument=instrument,
        timeframe=timeframe,
        ohlc=OHLC(
            open=as_price(open),
            high=as_price(high),
            low=as_price(low),
            close=as_price(close),
        ),
        volume=Quantity(value=as_decimal(volume if volume is not None else 0)),
        timestamp=timestamp,
    )


def candles_from_dataframe(
    instrument: Instrument, df: Any, *, timeframe: Timeframe = Timeframe.M1
) -> list[Candle]:
    """DataFrame (row.open/high/low/close/volume/timestamp) -> Candle list."""
    out: list[Candle] = []
    for row in df.itertuples(index=False):  # type: ignore[union-attr]
        out.append(
            make_candle(
                instrument,
                timeframe,
                open=row.open,
                high=row.high,
                low=row.low,
                close=row.close,
                volume=int(getattr(row, "volume", 0) or 0),
                timestamp=row.timestamp.to_pydatetime(),  # type: ignore[union-attr]
            )
        )
    return out


__all__ = ["candles_from_dataframe", "make_candle"]
