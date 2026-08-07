"""Tests for replay/optimization.py — grid_search and result objects."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tradex_trading.replay.optimization import (
    GridSearchResult,
    OptimizationResult,
    grid_search,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bt(
    total_return: float = 0.0,
    sharpe: float = 0.0,
    max_drawdown: float = 0.0,
    num_trades: int = 0,
):
    """Create a BacktestResult-like object without importing the real dataclass."""
    bt = MagicMock()
    bt.total_return = total_return
    bt.sharpe = sharpe
    bt.max_drawdown = max_drawdown
    bt.num_trades = num_trades
    return bt


# ---------------------------------------------------------------------------
# OptimizationResult
# ---------------------------------------------------------------------------

class TestOptimizationResult:
    def test_default_score_uses_total_return(self):
        bt = _make_bt(total_return=1.25)
        opt = OptimizationResult(params={"x": 1}, result=bt)
        assert opt.score == 1.25

    def test_explicit_score_preserved(self):
        bt = _make_bt(total_return=1.0)
        opt = OptimizationResult(params={"x": 1}, result=bt, score=99.0)
        assert opt.score == 99.0

    def test_zero_score_falls_back_to_total_return(self):
        bt = _make_bt(total_return=0.5)
        opt = OptimizationResult(params={}, result=bt, score=0.0)
        # 0.0 triggers __post_init__ fallback to total_return
        assert opt.score == 0.5


# ---------------------------------------------------------------------------
# GridSearchResult.top_n
# ---------------------------------------------------------------------------

class TestGridSearchResultTopN:
    def test_top_n_sorted_descending(self):
        r1 = OptimizationResult(params={"a": 1}, result=_make_bt(), score=10.0)
        r2 = OptimizationResult(params={"a": 2}, result=_make_bt(), score=30.0)
        r3 = OptimizationResult(params={"a": 3}, result=_make_bt(), score=20.0)
        gs = GridSearchResult(results=(r1, r2, r3))
        top = gs.top_n
        assert [r.score for r in top] == [30.0, 20.0, 10.0]

    def test_top_n_empty(self):
        gs = GridSearchResult(results=())
        assert gs.top_n == []


# ---------------------------------------------------------------------------
# grid_search
# ---------------------------------------------------------------------------

class TestGridSearch:
    def test_basic_grid(self):
        """Grid with 2x2 params should produce 4 combinations."""
        call_count = 0

        def run_fn(params):
            nonlocal call_count
            call_count += 1
            return _make_bt(total_return=params["a"] + params["b"])

        result = grid_search({"a": [1, 2], "b": [10, 20]}, run_fn)

        assert call_count == 4
        assert result.total_combinations == 4
        assert len(result.results) == 4
        assert result.best is not None
        assert result.best.params == {"a": 2, "b": 20}
        assert result.best.score == 22

    def test_custom_score_fn(self):
        """Custom score_fn should override default total_return scoring."""
        def run_fn(params):
            return _make_bt(total_return=params["x"] * 10, sharpe=params["x"] * 0.5)

        def score_fn(bt):
            return bt.sharpe  # score by sharpe, not total_return

        result = grid_search({"x": [1, 2, 3]}, run_fn, score_fn=score_fn)

        assert result.best is not None
        assert result.best.params == {"x": 3}
        assert result.best.score == 1.5  # 3 * 0.5

    def test_failing_combinations_skipped(self):
        """Exceptions in run_fn should be silently skipped."""
        def run_fn(params):
            if params["x"] == 2:
                raise ValueError("bad combo")
            return _make_bt(total_return=params["x"])

        result = grid_search({"x": [1, 2, 3]}, run_fn)

        assert result.total_combinations == 3
        assert len(result.results) == 2
        scores = {r.params["x"] for r in result.results}
        assert scores == {1, 3}

    def test_all_combinations_fail_returns_no_best(self):
        def run_fn(params):
            raise RuntimeError("always fail")

        result = grid_search({"x": [1, 2]}, run_fn)

        assert result.best is None
        assert len(result.results) == 0
        assert result.total_combinations == 2

    def test_single_value_grid(self):
        result = grid_search({"k": [42]}, lambda p: _make_bt(total_return=p["k"]))
        assert result.best.params == {"k": 42}
        assert result.best.score == 42
