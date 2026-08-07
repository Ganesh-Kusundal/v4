"""Gap tests for AnalyticsEngine — warmup trimming and sharpe with risk_free."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradex_domain import (
    OHLC,
    Candle,
    HistoricalSeries,
    Price,
    Quantity,
    Timeframe,
)
from tradex_domain.instruments import Equity

from tradex_trading.analytics.engine import AnalyticsEngine


def _eq() -> Equity:
    return Equity.of("NSE", "TEST")


def _candle(close: float, ts: datetime) -> Candle:
    return Candle(
        instrument=_eq(),
        timeframe=Timeframe.D1,
        ohlc=OHLC(
            open=Price(value=Decimal(str(close - 1))),
            high=Price(value=Decimal(str(close + 1))),
            low=Price(value=Decimal(str(close - 1))),
            close=Price(value=Decimal(str(close))),
        ),
        volume=Quantity(value=Decimal("1000")),
        timestamp=ts,
    )


def _series(closes: list[float]) -> HistoricalSeries:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    candles = [_candle(c, base + timedelta(days=i)) for i, c in enumerate(closes)]
    return HistoricalSeries(
        instrument=_eq(),
        timeframe=Timeframe.D1,
        candles=candles,
        start=base,
        end=base + timedelta(days=len(closes) - 1),
    )


def test_warmup_bars_exceeding_candle_count_trims() -> None:
    """When warmup_bars > candle count, output is trimmed to candle count."""
    engine = AnalyticsEngine(warmup_bars=50)
    series = _series([100.0, 101.0, 102.0, 103.0, 104.0])
    result = engine.indicator(series, "sma", period=3)
    # Output should have at most len(series.candles) candles
    assert len(result.candles) <= len(series.candles)


def test_report_sharpe_with_custom_risk_free_rate() -> None:
    """Sharpe report accepts risk_free parameter."""
    engine = AnalyticsEngine()
    series = _series([100.0, 102.0, 104.0, 103.0, 106.0, 108.0])
    result_default = engine.report("sharpe", series)
    result_rf = engine.report("sharpe", series, risk_free=0.05)
    # Both should return a dict with 'sharpe' key
    assert "sharpe" in result_default
    assert "sharpe" in result_rf
    # With positive risk_free rate, sharpe should be lower
    assert result_rf["sharpe"] < result_default["sharpe"]
