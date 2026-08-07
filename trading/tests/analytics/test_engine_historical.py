"""Analytics engine tests — indicator(), indicators(), report(), _with_close() (D-15).

Tests the HistoricalSeries-based API ported from v3.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tradex_domain import (
    OHLC,
    Candle,
    Equity,
    HistoricalSeries,
    Price,
    Quantity,
    Timeframe,
)
from tradex_domain.errors import CapabilityNotSupportedError

from tradex_trading.analytics.engine import AnalyticsEngine


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


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


# ---------------------------------------------------------------------------
# indicator()
# ---------------------------------------------------------------------------


class TestIndicator:
    """AnalyticsEngine.indicator(series, name, **params)."""

    def test_sma_replaces_trailing_closes(self) -> None:
        engine = AnalyticsEngine()
        series = _series([float(i) for i in range(1, 25)])
        result = engine.indicator(series, "sma")
        # SMA(20) on 24 values → 5 non-None values → 5 trailing candles replaced
        assert len(result.candles) == 5
        # First SMA value = avg(1..20) = 10.5
        assert float(result.candles[0].ohlc.close.value) == pytest.approx(10.5)

    def test_unknown_indicator_raises(self) -> None:
        engine = AnalyticsEngine()
        series = _series([1.0, 2.0, 3.0])
        with pytest.raises(CapabilityNotSupportedError, match="not implemented"):
            engine.indicator(series, "bogus")

    def test_custom_period(self) -> None:
        engine = AnalyticsEngine()
        series = _series([float(i) for i in range(1, 10)])
        result = engine.indicator(series, "sma", period=3)
        # SMA(3) on 9 values → 7 non-None → 7 trailing candles
        assert len(result.candles) == 7


# ---------------------------------------------------------------------------
# indicators()
# ---------------------------------------------------------------------------


class TestIndicators:
    """AnalyticsEngine.indicators(series, names, **params)."""

    def test_chains_multiple_indicators(self) -> None:
        engine = AnalyticsEngine()
        series = _series([float(i) for i in range(1, 30)])
        result = engine.indicators(series, ["sma", "sma"], period=5)
        # After first SMA(5): 25 candles, after second SMA(5) on those 25: 21 candles
        assert len(result.candles) > 0


# ---------------------------------------------------------------------------
# report()
# ---------------------------------------------------------------------------


class TestReport:
    """AnalyticsEngine.report(name, series, **params)."""

    def test_max_drawdown(self) -> None:
        engine = AnalyticsEngine()
        series = _series([100.0, 90.0, 95.0, 70.0])
        result = engine.report("max_drawdown", series)
        assert "max_drawdown" in result
        assert result["max_drawdown"] == pytest.approx(-0.30)

    def test_sharpe(self) -> None:
        engine = AnalyticsEngine()
        series = _series([100.0, 101.0, 102.0, 103.0, 104.0])
        result = engine.report("sharpe", series)
        assert "sharpe" in result

    def test_realized_vol(self) -> None:
        engine = AnalyticsEngine()
        series = _series([100.0 * (1.01 ** i) for i in range(20)])
        result = engine.report("realized_vol", series)
        assert result["realized_vol"] > 0.0

    def test_win_rate(self) -> None:
        engine = AnalyticsEngine()
        series = _series([10.0, 11.0, 9.0, 12.0])
        result = engine.report("win_rate", series)
        assert "win_rate" in result

    def test_advance_decline(self) -> None:
        engine = AnalyticsEngine()
        series = _series([100.0, 101.0, 100.5, 102.0])
        result = engine.report("advance_decline", series)
        assert "advances" in result
        assert "declines" in result

    def test_unknown_report_raises(self) -> None:
        engine = AnalyticsEngine()
        series = _series([1.0, 2.0])
        with pytest.raises(CapabilityNotSupportedError, match="not implemented"):
            engine.report("bogus", series)


# ---------------------------------------------------------------------------
# _with_close()
# ---------------------------------------------------------------------------


class TestWithClose:
    """AnalyticsEngine._with_close(series, values)."""

    def test_empty_values_returns_empty_candles(self) -> None:
        series = _series([1.0, 2.0, 3.0])
        result = AnalyticsEngine._with_close(series, [])
        assert result.candles == []

    def test_replaces_trailing_closes(self) -> None:
        series = _series([10.0, 20.0, 30.0, 40.0, 50.0])
        result = AnalyticsEngine._with_close(series, [99.0, 99.0])
        assert len(result.candles) == 2
        assert float(result.candles[0].ohlc.close.value) == pytest.approx(99.0)
        assert float(result.candles[1].ohlc.close.value) == pytest.approx(99.0)
        # Other OHLC fields preserved
        assert result.candles[0].ohlc.open == series.candles[-2].ohlc.open
