"""Tests for walk-forward analysis engine."""

from __future__ import annotations

import pytest

from tradex_trading.replay.backtest import BacktestResult
from tradex_trading.replay.walk_forward import (
    WalkForwardReport,
    WalkForwardStep,
    run_walk_forward,
)


def _make_bt(total_return: float = 0.0, sharpe: float = 0.0) -> BacktestResult:
    return BacktestResult(
        total_return=total_return,
        sharpe=sharpe,
        max_drawdown=0.0,
        num_trades=0,
    )


class TestWalkForwardStep:
    def test_defaults(self):
        step = WalkForwardStep(
            step_index=0, train_start=0, train_end=100,
            test_start=100, test_end=150,
        )
        assert step.best_params == {}
        assert step.in_sample_result is None
        assert step.out_of_sample_result is None
        assert step.in_sample_score == 0.0
        assert step.out_of_sample_score == 0.0


class TestWalkForwardReport:
    def test_empty_report(self):
        report = WalkForwardReport(steps=())
        assert report.total_steps == 0
        assert report.successful_steps == 0
        assert report.success_rate == 0.0

    def test_success_rate(self):
        report = WalkForwardReport(steps=(), total_steps=10, successful_steps=7)
        assert report.success_rate == 0.7

    def test_summary(self):
        report = WalkForwardReport(
            steps=(), total_steps=3, successful_steps=2,
            aggregate_oos_return=0.05, aggregate_oos_sharpe=1.2,
        )
        s = report.summary()
        assert "WALK-FORWARD" in s
        assert "Total Steps" in s


class TestRunWalkForward:
    def test_basic(self):
        data = list(range(400))

        def run_fn(params, subset):
            return _make_bt(total_return=0.01 * len(params))

        def optimize_fn(train_data):
            return {"fast": 10}

        report = run_walk_forward(
            data=data,
            run_fn=run_fn,
            optimize_fn=optimize_fn,
            train_window=252,
            test_window=63,
        )
        assert report.total_steps > 0
        assert report.successful_steps > 0

    def test_insufficient_data(self):
        with pytest.raises(ValueError, match="at least"):
            run_walk_forward(
                data=list(range(10)),
                run_fn=lambda p, d: _make_bt(),
                optimize_fn=lambda d: {},
                train_window=252,
                test_window=63,
            )

    def test_custom_step_size(self):
        data = list(range(500))

        def run_fn(params, subset):
            return _make_bt(total_return=0.01)

        def optimize_fn(train_data):
            return {"p": 1}

        report = run_walk_forward(
            data=data,
            run_fn=run_fn,
            optimize_fn=optimize_fn,
            train_window=200,
            test_window=50,
            step_size=25,
        )
        assert report.total_steps > 1

    def test_optimize_failure_handled(self):
        data = list(range(400))

        def run_fn(params, subset):
            return _make_bt(total_return=0.01)

        def optimize_fn(train_data):
            raise RuntimeError("optimize failed")

        report = run_walk_forward(
            data=data,
            run_fn=run_fn,
            optimize_fn=optimize_fn,
            train_window=252,
            test_window=63,
        )
        # Should still complete, with empty best_params
        assert report.total_steps > 0

    def test_run_fn_failure_handled(self):
        data = list(range(400))

        def run_fn(params, subset):
            raise RuntimeError("run failed")

        def optimize_fn(train_data):
            return {"p": 1}

        report = run_walk_forward(
            data=data,
            run_fn=run_fn,
            optimize_fn=optimize_fn,
            train_window=252,
            test_window=63,
        )
        assert report.total_steps > 0
        assert report.successful_steps == 0

    def test_custom_score_fn(self):
        data = list(range(400))

        def run_fn(params, subset):
            return _make_bt(total_return=0.05, sharpe=1.5)

        def optimize_fn(train_data):
            return {"p": 1}

        report = run_walk_forward(
            data=data,
            run_fn=run_fn,
            optimize_fn=optimize_fn,
            train_window=252,
            test_window=63,
            score_fn=lambda r: r.sharpe,
        )
        assert report.aggregate_oos_sharpe == 1.5
