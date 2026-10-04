"""AnalyticsView: cached result equals the direct indicator computation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradex_analytics.indicators import compute_indicator
from tradex_analytics.registry import REGISTRY
from tradex_analytics.runtime import AnalyticsRuntime
from tradex_domain import OHLC, Candle, Equity, Price, Quantity, Timeframe


def _candle(symbol: str, i: int) -> Candle:
    ts = datetime(2026, 8, 5, 15, 0, tzinfo=UTC) + timedelta(minutes=i)
    return Candle(
        instrument=Equity.of("NSE", symbol),
        timeframe=Timeframe.M1,
        ohlc=OHLC(
            open=Price(value=Decimal("100")),
            high=Price(value=Decimal("101")),
            low=Price(value=Decimal("99")),
            close=Price(value=Decimal(str(100 + i))),
        ),
        volume=Quantity(value=Decimal("1000")),
        timestamp=ts,
    )


def test_view_indicator_matches_direct_computation() -> None:
    rt = AnalyticsRuntime()
    for i in range(30):
        rt.ingest(_candle("RELIANCE", i))
    inst = Equity.of("NSE", "RELIANCE").instrument_id
    view = rt.view(inst, Timeframe.M1)
    got = view.indicator("sma", period=5)
    series = rt._store.series(inst, Timeframe.M1)
    expected = compute_indicator("sma", series.candles, {"period": 5}, REGISTRY)
    assert got["value"] == expected["value"]


def test_view_indicator_none_before_warmup() -> None:
    rt = AnalyticsRuntime()
    for i in range(3):
        rt.ingest(_candle("RELIANCE", i))
    inst = Equity.of("NSE", "RELIANCE").instrument_id
    view = rt.view(inst, Timeframe.M1)
    got = view.indicator("sma", period=5)
    assert got["value"][-1] is None  # not enough bars yet
