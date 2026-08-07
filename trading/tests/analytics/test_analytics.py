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
    max_drawdown,
    pe_ratio,
    poc,
    rank_by_return,
    realized_vol,
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
