"""Tests for analytics tearsheet and summary."""

from __future__ import annotations


class TestTearsheet:
    def _engine(self):
        from tradex_trading.analytics.engine import AnalyticsEngine
        return AnalyticsEngine()

    def test_tearsheet_empty_returns(self):
        eng = self._engine()
        result = eng.tearsheet([])
        assert result["total_return"] == 0.0
        assert result["num_trades"] == 0

    def test_tearsheet_none_returns(self):
        eng = self._engine()
        result = eng.tearsheet(None)
        assert result["total_return"] == 0.0

    def test_tearsheet_with_returns(self):
        eng = self._engine()
        returns = [0.01, -0.005, 0.02, 0.01, -0.01]
        result = eng.tearsheet(returns)
        assert "total_return" in result
        assert "sharpe_ratio" in result
        assert "max_drawdown" in result
        assert "volatility" in result
        assert "sortino_ratio" in result
        assert "calmar_ratio" in result
        assert "win_rate" in result
        assert "profit_factor" in result
        assert result["num_trades"] == 5

    def test_tearsheet_win_rate(self):
        eng = self._engine()
        returns = [0.01, 0.02, -0.01, 0.03]  # 3 wins, 1 loss
        result = eng.tearsheet(returns)
        assert result["win_rate"] == 0.75  # 3/4

    def test_tearsheet_all_wins(self):
        eng = self._engine()
        returns = [0.01, 0.02, 0.03]
        result = eng.tearsheet(returns)
        assert result["win_rate"] == 1.0
        assert result["profit_factor"] == float("inf")

    def test_tearsheet_all_losses(self):
        eng = self._engine()
        returns = [-0.01, -0.02, -0.03]
        result = eng.tearsheet(returns)
        assert result["win_rate"] == 0.0


class TestSummary:
    def _engine(self):
        from tradex_trading.analytics.engine import AnalyticsEngine
        return AnalyticsEngine()

    def test_summary_returns_string(self):
        eng = self._engine()
        result = eng.summary([0.01, -0.005, 0.02])
        assert isinstance(result, str)

    def test_summary_contains_metrics(self):
        eng = self._engine()
        result = eng.summary([0.01, -0.005, 0.02])
        assert "Total Return" in result
        assert "Sharpe Ratio" in result
        assert "Max Drawdown" in result
        assert "Win Rate" in result

    def test_summary_empty(self):
        eng = self._engine()
        result = eng.summary([])
        assert isinstance(result, str)
        assert "PERFORMANCE SUMMARY" in result
