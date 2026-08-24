"""Single-source market builders (SMELL-04 + SMELL-05).

- ``build_depth``: the sorted(bids desc)/sorted(asks asc) + (Price,Quantity)
  contract was copy-pasted across dhan/_marketdata, upstox/_marketdata,
  dhan/depth_parser, common/ws_shared, upstox/ws_streams.
- ``make_candle`` / ``candles_from_dataframe``: OHLC + Quantity coercion was
  duplicated in dhan/_marketdata, upstox/_marketdata:_candles_from_rows, and
  trading/datalake/market_provider:_to_candles.

Ponytail: 3 tiny functions, stdlib except domain types.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tradex_domain.enums import Timeframe
from tradex_domain.instruments import Instrument
from tradex_domain.market import OHLC, Candle, Depth
from tradex_domain.value_objects import Price, Quantity

from tradex_brokers.common.provider_common import as_decimal, as_price


def make_candle(
    instrument: Instrument,
    timeframe: Timeframe,
    *,
    open: Any,
    high: Any,
    low: Any,
    close: Any,
    volume: Any = 0,
    timestamp: datetime,
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


def build_depth(
    instrument: Instrument,
    *,
    bids_raw: list[dict[str, Any]] | None = None,
    asks_raw: list[dict[str, Any]] | None = None,
    depth_data: dict[str, Any] | None = None,
    price_key: str = "price",
    qty_key: str = "quantity",
    timestamp: datetime | None = None,
) -> Depth:
    """Build a sorted, validated Depth from raw provider levels."""

    def _level(d: dict[str, Any]) -> tuple[Price, Quantity]:
        q = d.get(qty_key, 0)
        raw_qty = str(q) if q is not None else 0
        return as_price(d.get(price_key)), Quantity(value=as_decimal(raw_qty))

    if depth_data is not None:
        bids_raw = [x for x in depth_data.get("buy", []) if isinstance(x, dict)]
        asks_raw = [x for x in depth_data.get("sell", []) if isinstance(x, dict)]
    bids_raw = bids_raw or []
    asks_raw = asks_raw or []
    bids = tuple(
        sorted(
            (_level(x) for x in bids_raw if isinstance(x, dict)),
            key=lambda lv: lv[0].value,
            reverse=True,
        )
    )
    asks = tuple(
        sorted(
            (_level(x) for x in asks_raw if isinstance(x, dict)),
            key=lambda lv: lv[0].value,
        )
    )
    return Depth(
        instrument=instrument,
        bids=bids,
        asks=asks,
        timestamp=timestamp or datetime.now(UTC),
    )


__all__ = ["build_depth", "candles_from_dataframe", "make_candle"]
