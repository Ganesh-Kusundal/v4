"""HistoricalSeries contract tests — ported from v3.

Covers the time-series operations available in v4: resample/_bucketize.

Tests for v3-only methods (to_dataframe, to_polars, to_arrow, slice, window,
rolling, indicator, indicators, stream) are intentionally omitted — those
methods do not exist in v4's HistoricalSeries.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradex_domain import (
    OHLC,
    Candle,
    Equity,
    HistoricalSeries,
    Price,
    Quantity,
    Timeframe,
)


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _candle(i: int, *, base: datetime) -> Candle:
    close = Decimal(10 + i)
    return Candle(
        instrument=_eq(),
        timeframe=Timeframe.M1,
        ohlc=OHLC(
            open=Price(value=close - Decimal(1)),
            high=Price(value=close + Decimal(2)),
            low=Price(value=close - Decimal(3)),
            close=Price(value=close),
        ),
        volume=Quantity(value=Decimal(100 * (i + 1))),
        timestamp=base + timedelta(minutes=i),
    )


def _series(n: int = 5, *, base: datetime | None = None) -> HistoricalSeries:
    start = base or datetime(2026, 7, 31, 9, 0, tzinfo=UTC)
    candles = [_candle(i, base=start) for i in range(n)]
    return HistoricalSeries(
        instrument=_eq(),
        timeframe=Timeframe.M1,
        candles=candles,
        start=candles[0].timestamp,
        end=candles[-1].timestamp,
    )


# ---------------------------------------------------------------------------
# resample / _bucketize
# ---------------------------------------------------------------------------


def test_resample_m1_to_m5_aggregates() -> None:
    series = _series(5)  # 09:00..09:04 M1
    resampled = series.resample(Timeframe.M5)
    assert len(resampled.candles) == 1
    candle = resampled.candles[0]
    # open = first open, close = last close
    assert candle.ohlc.open.value == 9
    assert candle.ohlc.close.value == 14
    # high/low are Price objects per the OHLC contract (not raw Decimals)
    assert isinstance(candle.ohlc.high, Price)
    assert isinstance(candle.ohlc.low, Price)
    assert candle.ohlc.high.value == 16  # max of (12..16)
    assert candle.ohlc.low.value == 7  # min of (7..11)
    assert candle.volume.value == Decimal(100 * (1 + 2 + 3 + 4 + 5))


def test_resample_crosses_bucket_boundary() -> None:
    series = _series(6)  # 09:00..09:05 => two M5 buckets
    resampled = series.resample(Timeframe.M5)
    assert len(resampled.candles) == 2
    assert resampled.candles[0].volume.value == Decimal(100 * (1 + 2 + 3 + 4 + 5))
    assert resampled.candles[1].volume.value == Decimal(600)


def test_resample_preserves_timeframe() -> None:
    resampled = _series(5).resample(Timeframe.H1)
    assert resampled.timeframe is Timeframe.H1
