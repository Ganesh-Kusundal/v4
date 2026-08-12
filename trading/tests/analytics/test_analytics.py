"""Analytics tests — engine compute + standalone functions (F15).

Ported from v3 ``test_analytics_reports.py`` + analytics parts of
``test_analytics_datalake.py``.

v4 API differences:
- ``AnalyticsEngine.compute(series, indicators)`` → dict (v3 had report/indicator/indicators)
- Standalone functions: sma, ema, rsi, sharpe_ratio, max_drawdown, total_return, etc.
- sma/ema/rsi return None-padded lists (same length as input), not trailing-only.
"""

from __future__ import annotations

import pytest

from tradex_trading.analytics import (
    AnalyticsEngine,
    advance_decline,
    basis,
    black_scholes_call,
    ema,
    imbalance,
    intrinsic_call,
    macd,
    max_drawdown,
    pe_ratio,
    poc,
    rank_by_return,
    realized_vol,
    roc,
    rsi,
    sector_strength,
    sharpe_ratio,
    sma,
    split_windows,
    total_return,
    win_rate,
)

# ---------------------------------------------------------------------------
# AnalyticsEngine.compute()
# ---------------------------------------------------------------------------


class TestAnalyticsEngine:
    """AnalyticsEngine.compute(series, indicators) → dict."""

    def test_compute_sma(self) -> None:
        engine = AnalyticsEngine()
        values = [float(i) for i in range(1, 25)]
        result = engine.compute(values, ["sma"])
        assert "sma" in result
        assert len(result["sma"]) == len(values)
        # First 19 values should be None (period=20)
        assert result["sma"][0] is None
        # Value at index 19 should be the average of 1..20
        assert result["sma"][19] == pytest.approx(10.5)

    def test_compute_ema(self) -> None:
        engine = AnalyticsEngine()
        values = [float(i) for i in range(1, 25)]
        result = engine.compute(values, ["ema"])
        assert "ema" in result
        assert len(result["ema"]) == len(values)
        assert result["ema"][0] is None
        # First non-None value is SMA seed
        assert result["ema"][19] == pytest.approx(10.5)

    def test_compute_rsi(self) -> None:
        engine = AnalyticsEngine()
        values = [float(i) for i in range(1, 20)]
        result = engine.compute(values, ["rsi"])
        assert "rsi" in result
        # Monotonic uptrend → RSI = 100
        non_none = [v for v in result["rsi"] if v is not None]
        assert non_none[-1] == pytest.approx(100.0)

    def test_compute_multiple_indicators(self) -> None:
        engine = AnalyticsEngine()
        values = [float(i) for i in range(1, 25)]
        result = engine.compute(values, ["sma", "ema", "rsi"])
        assert set(result.keys()) == {"sma", "ema", "rsi"}

    def test_compute_roc(self) -> None:
        engine = AnalyticsEngine()
        values = [100.0] * 10 + [110.0] * 4
        result = engine.compute(values, ["roc"])
        non_none = [v for v in result["roc"] if v is not None]
        assert non_none
        assert non_none[-1] == pytest.approx(10.0)

    def test_compute_macd(self) -> None:
        engine = AnalyticsEngine()
        values = [float(i) for i in range(1, 30)]
        result = engine.compute(values, ["macd"])
        non_none = [v for v in result["macd"] if v is not None]
        assert non_none

    def test_indicator_roc_via_engine(self) -> None:
        """ScannerEngine's indicator() path supports roc conditions."""
        from datetime import UTC, datetime, timedelta
        from decimal import Decimal

        from tradex_domain import OHLC, Candle
        from tradex_domain.instruments import Equity
        from tradex_domain.market import HistoricalSeries
        from tradex_domain.value_objects import Price, Quantity

        engine = AnalyticsEngine()
        inst = Equity.of("NSE", "RELIANCE")
        candles = [
            Candle(
                instrument=inst, timeframe="1d",  # type: ignore[arg-type]
                ohlc=OHLC(
                    open=Price(value=Decimal(str(c))),
                    high=Price(value=Decimal(str(c))),
                    low=Price(value=Decimal(str(c))),
                    close=Price(value=Decimal(str(c))),
                ),
                volume=Quantity(value=Decimal("1000")),
                timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(days=i),
            )
            for i, c in enumerate([100.0] * 10 + [110.0, 110.0, 110.0, 110.0])
        ]
        series = HistoricalSeries(
            instrument=inst, timeframe="1d",  # type: ignore[arg-type]
            candles=candles, start=candles[0].timestamp, end=candles[-1].timestamp,
        )
        out = engine.indicator(series, "roc", period=10)
        assert out.candles
        assert float(out.candles[-1].ohlc.close.value) == pytest.approx(10.0)

    def test_compute_unknown_indicator_raises(self) -> None:
        engine = AnalyticsEngine()
        with pytest.raises(ValueError, match="Unknown indicator"):
            engine.compute([1.0, 2.0], ["bogus"])


# ---------------------------------------------------------------------------
# Technical indicators — standalone functions
# ---------------------------------------------------------------------------


class TestIndicators:
    """sma, ema, rsi standalone functions."""

    def test_sma_returns_same_length(self) -> None:
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = sma(values, 3)
        assert len(result) == 5
        # First 2 are None (period-1 padding)
        assert result[0] is None
        assert result[1] is None
        assert result[2] == pytest.approx(2.0)

    def test_sma_insufficient_data(self) -> None:
        result = sma([1.0, 2.0], 5)
        assert all(v is None for v in result)

    def test_sma_rejects_zero_period(self) -> None:
        with pytest.raises(ValueError):
            sma([1.0], 0)

    def test_ema_is_exponentially_weighted(self) -> None:
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = ema(values, 3)
        assert len(result) == 5
        assert result[0] is None
        assert result[1] is None
        assert result[2] == pytest.approx(2.0)  # seed = SMA
        assert result[-1] < 5.0

    def test_rsi_monotonic_uptrend(self) -> None:
        values = [float(i) for i in range(1, 20)]
        result = rsi(values, 14)
        assert len(result) == 19
        non_none = [v for v in result if v is not None]
        assert non_none[-1] == pytest.approx(100.0)

    def test_rsi_insufficient_data(self) -> None:
        result = rsi([1.0, 2.0], 14)
        assert all(v is None for v in result)

    def test_roc_monotonic_uptrend(self) -> None:
        values = [100.0, 110.0, 121.0]
        result = roc(values, 1)
        assert len(result) == 3
        assert result[0] is None
        assert result[1] == pytest.approx(10.0)
        assert result[2] == pytest.approx(10.0)

    def test_roc_period_ten_linear_ramp(self) -> None:
        # 100..113 ramp: value at t vs value at t-10 → 10% at every non-None point
        values = [100.0] * 10 + [110.0] * 4
        result = roc(values, 10)
        non_none = [v for v in result if v is not None]
        assert non_none == [pytest.approx(10.0)] * 4

    def test_roc_insufficient_data(self) -> None:
        result = roc([1.0, 2.0], 5)
        assert all(v is None for v in result)

    def test_roc_flat_base_is_zero(self) -> None:
        result = roc([0.0, 5.0], 1)
        assert result[1] == 0.0

    def test_roc_rejects_zero_period(self) -> None:
        with pytest.raises(ValueError):
            roc([1.0, 2.0], 0)

    def test_macd_is_fast_ema_minus_slow_ema(self) -> None:
        values = [float(i) for i in range(1, 30)]
        result = macd(values, 8)  # fast=4, slow=8
        assert len(result) == len(values)
        # Leading values None-padded (slow period - 1)
        assert result[0] is None
        non_none = [v for v in result if v is not None]
        assert non_none
        # For a linear ramp, EMA lag ≈ (period-1)/2, so MACD ≈ (8-4)/2 = 2.0
        assert non_none[-1] == pytest.approx(2.0, abs=0.2)

    def test_macd_rejects_small_period(self) -> None:
        with pytest.raises(ValueError):
            macd([1.0, 2.0, 3.0], 1)


# ---------------------------------------------------------------------------
# Performance reports
# ---------------------------------------------------------------------------


class TestReports:
    """sharpe_ratio, max_drawdown, total_return."""

    def test_sharpe_flat_is_zero(self) -> None:
        assert sharpe_ratio([1.0] * 5) == 0.0

    def test_sharpe_rising_is_positive(self) -> None:
        assert sharpe_ratio([100.0, 101.0, 102.0, 103.0, 104.0]) > 0.0

    def test_sharpe_empty(self) -> None:
        assert sharpe_ratio([]) == 0.0

    def test_max_drawdown(self) -> None:
        # v4 returns negative drawdown (e.g. -0.30 for a 30% drop)
        assert max_drawdown([100.0, 90.0, 95.0, 70.0]) == pytest.approx(-0.30)

    def test_max_drawdown_empty(self) -> None:
        assert max_drawdown([]) == 0.0

    def test_total_return(self) -> None:
        assert total_return([100.0, 110.0]) == pytest.approx(0.10)

    def test_total_return_empty(self) -> None:
        assert total_return([]) == 0.0


# ---------------------------------------------------------------------------
# Other analytics functions
# ---------------------------------------------------------------------------


class TestOtherAnalytics:
    """realized_vol, advance_decline, win_rate, imbalance, etc."""

    def test_realized_vol_flat_is_zero(self) -> None:
        assert realized_vol([100.0] * 10) == 0.0

    def test_realized_vol_trend_is_positive(self) -> None:
        prices = [100.0 * (1.01**i) for i in range(20)]
        assert realized_vol(prices) > 0.0

    def test_advance_decline(self) -> None:
        advances, declines, ratio = advance_decline([1.0, -1.0, 0.5, 0.0])
        assert advances == 2
        assert declines == 1
        assert ratio == pytest.approx(2.0)

    def test_win_rate(self) -> None:
        assert win_rate([1.0, -1.0, 2.0]) == pytest.approx(2 / 3)

    def test_win_rate_empty(self) -> None:
        assert win_rate([]) == 0.0

    def test_imbalance(self) -> None:
        assert imbalance(bid_size=30.0, ask_size=10.0) == pytest.approx(0.5)

    def test_imbalance_zero(self) -> None:
        assert imbalance(0.0, 0.0) == 0.0

    def test_sector_strength(self) -> None:
        result = sector_strength({"A": 0.1, "B": 0.5})
        assert result == [("B", 0.5), ("A", 0.1)]

    def test_volume_profile_poc(self) -> None:
        assert poc({100.0: 10, 101.0: 50, 102.0: 20}) == 101.0

    def test_poc_empty(self) -> None:
        assert poc({}) is None

    def test_walk_forward_split_windows(self) -> None:
        windows = split_windows(n=100, train=40, test=20)
        assert windows == [(0, 40, 40, 60), (20, 60, 60, 80), (40, 80, 80, 100)]

    def test_ranking(self) -> None:
        assert rank_by_return({"A": 0.1, "B": 0.5, "C": 0.2}) == ["B", "C", "A"]

    def test_pe_ratio(self) -> None:
        assert pe_ratio(price=100.0, eps=10.0) == pytest.approx(10.0)

    def test_pe_ratio_zero_eps_raises(self) -> None:
        with pytest.raises(ValueError):
            pe_ratio(price=100.0, eps=0.0)

    def test_basis(self) -> None:
        assert basis(105.0, 100.0) == pytest.approx(5.0)

    def test_options_intrinsic(self) -> None:
        assert intrinsic_call(spot=105.0, strike=100.0) == pytest.approx(5.0)
        assert intrinsic_call(spot=95.0, strike=100.0) == 0.0

    def test_black_scholes_call(self) -> None:
        assert black_scholes_call(
            spot=100.0, strike=100.0, time_years=1.0, rate=0.0, sigma=0.2,
        ) > 0.0
