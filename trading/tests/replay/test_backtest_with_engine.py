"""Tests for BacktestEngine with ExecutionEngine integration."""

from __future__ import annotations

from unittest.mock import MagicMock

from tradex_trading.replay.backtest import BacktestEngine


class TestBacktestEngineWithEngine:
    """Tests for BacktestEngine with optional engine parameter."""

    def test_without_engine_uses_simplified_path(self) -> None:
        """Without engine, uses simplified fill_source or returns request."""
        engine = BacktestEngine()
        request = MagicMock()
        result = engine.submit(request)
        # Without fill_source or engine, returns the request itself
        assert result is request

    def test_without_engine_uses_fill_source(self) -> None:
        """Without engine but with fill_source, delegates to fill_source."""
        fill_source = MagicMock()
        fill_source.submit.return_value = "fill_result"
        engine = BacktestEngine(fill_source=fill_source)
        request = MagicMock()
        result = engine.submit(request)
        assert result == "fill_result"
        fill_source.submit.assert_called_once_with(request)

    def test_with_engine_delegates_to_engine_submit(self) -> None:
        """With engine parameter, delegates to engine.submit()."""
        mock_engine = MagicMock()
        mock_engine.submit.return_value = "engine_result"
        backtest = BacktestEngine(engine=mock_engine)
        request = MagicMock()
        result = backtest.submit(request)
        assert result == "engine_result"
        mock_engine.submit.assert_called_once_with(request)

    def test_engine_takes_precedence_over_fill_source(self) -> None:
        """Engine takes precedence over fill_source."""
        mock_engine = MagicMock()
        mock_engine.submit.return_value = "engine_result"
        fill_source = MagicMock()
        fill_source.submit.return_value = "fill_result"
        backtest = BacktestEngine(fill_source=fill_source, engine=mock_engine)
        request = MagicMock()
        result = backtest.submit(request)
        assert result == "engine_result"
        mock_engine.submit.assert_called_once_with(request)
        fill_source.submit.assert_not_called()
