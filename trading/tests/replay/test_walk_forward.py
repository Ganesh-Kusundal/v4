"""Tests for walk-forward analysis engine."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from tradex_domain import OHLC, Candle, Price, Quantity, Timeframe
from tradex_domain.instruments import Equity

from tradex_trading.replay.backtest import BacktestResult
from tradex_trading.replay.optimization import make_grid_optimizer
from tradex_trading.replay.walk_forward import (
    WalkForwardReport,
    WalkForwardStep,
    run_walk_forward,
    run_walk_forward_by_date,
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


def _candle(ts: datetime, symbol: str = "RELIANCE") -> Candle:
    """A minimal D1 candle for date-windowed walk-forward tests."""
    price = Price(value=Decimal("100"))
    return Candle(
        instrument=Equity.of("NSE", symbol),
        timeframe=Timeframe.D1,
        ohlc=OHLC(open=price, high=price, low=price, close=price),
        volume=Quantity(value=Decimal("1000")),
        timestamp=ts,
    )


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

    def test_grid_optimizer_composition(self):
        """make_grid_optimizer plugs into run_walk_forward as optimize_fn."""
        data = list(range(400))

        def run_fn(params, subset):
            return _make_bt(total_return=params.get("k", 1) * 0.01)

        optimize_fn = make_grid_optimizer({"k": [1, 2, 3]}, run_fn)
        report = run_walk_forward(
            data=data,
            run_fn=run_fn,
            optimize_fn=optimize_fn,
            train_window=252,
            test_window=63,
        )
        assert report.total_steps > 0
        assert report.successful_steps > 0
        for step in report.steps:
            assert step.best_params["k"] == 3  # grid optimizer found the best


class TestRunWalkForwardByDate:
    """Calendar-windowed walk-forward over (multi-symbol) candle data."""

    def test_basic(self):
        candles = [
            _candle(datetime(2026, 1, 1) + timedelta(days=day))
            for day in range(10)
        ]
        report = run_walk_forward_by_date(
            candles, lambda p, d: _make_bt(total_return=0.01), lambda d: {},
            train_days=4, test_days=2,
        )
        assert report.total_steps == 3
        assert report.successful_steps == 3

    def test_empty_data_raises(self):
        with pytest.raises(ValueError, match="empty"):
            run_walk_forward_by_date(
                [], lambda p, d: _make_bt(), lambda d: {},
            )

    def test_date_windows_require_distinct_dates(self):
        """20 bars over only 5 dates → no steps (bar count is irrelevant)."""
        candles = [
            _candle(datetime(2026, 1, 1) + timedelta(days=day))
            for day in range(5)
            for _ in range(4)
        ]
        report = run_walk_forward_by_date(
            candles, lambda p, d: _make_bt(), lambda d: {},
            train_days=4, test_days=2,
        )
        assert report.total_steps == 0

    def test_windows_span_all_symbols(self):
        """Interleaved multi-symbol data: each window contains both symbols —
        the property that makes portfolio walk-forward meaningful."""
        seen: set[str] = set()

        def run_fn(params, subset):
            seen.update(c.instrument.instrument_id for c in subset)
            return _make_bt(total_return=0.01)

        candles = []
        for day in range(8):
            ts = datetime(2026, 1, 1) + timedelta(days=day)
            candles.append(_candle(ts, "RELIANCE"))
            candles.append(_candle(ts, "TCS"))

        report = run_walk_forward_by_date(
            candles, run_fn, lambda d: {},
            train_days=4, test_days=2,
        )
        assert report.total_steps == 2
        assert {str(i) for i in seen} == {"NSE:RELIANCE", "NSE:TCS"}


def test_walk_forward_stitched_oos_curve():
    """C2: _aggregate must stitch equity segments, not average returns; MaxDD must exist."""
    from tradex_trading.analytics.reports import max_drawdown, sharpe_ratio
    from tradex_trading.replay.walk_forward import WalkForwardStep, _aggregate

    seg1 = [1.0, 1.10]
    seg2 = [1.0, 1.20]
    seg3 = [1.0, 0.90]

    def _bt(tr: float, sh: float, curve: list[float]) -> BacktestResult:
        return BacktestResult(
            total_return=tr, sharpe=sh, max_drawdown=0.0, num_trades=0, equity_curve=curve
        )

    steps = [
        WalkForwardStep(
            step_index=0, train_start=0, train_end=10, test_start=10, test_end=11,
            out_of_sample_result=_bt(0.10, 1.0, seg1),
        ),
        WalkForwardStep(
            step_index=1, train_start=10, train_end=20, test_start=20, test_end=21,
            out_of_sample_result=_bt(0.20, 2.0, seg2),
        ),
        WalkForwardStep(
            step_index=2, train_start=20, train_end=30, test_start=30, test_end=31,
            out_of_sample_result=_bt(-0.10, -1.0, seg3),
        ),
    ]
    agg_return, agg_sharpe, successful, agg_maxdd = _aggregate(steps)
    # old mean return
    mean_return = (0.10 + 0.20 - 0.10) / 3
    assert agg_return != pytest.approx(mean_return), "stitched return must differ from mean"
    # stitched: 1.0 ->1.10 ->1.32 ->1.188
    assert agg_return == pytest.approx(0.188, rel=1e-6)
    # sharpe must be from stitched returns, not mean
    assert agg_sharpe != pytest.approx((1.0 + 2.0 - 1.0) / 3)
    stitched = [1.0, 1.10, 1.32, 1.188]
    rets = [
        (stitched[i] - stitched[i - 1]) / stitched[i - 1]
        for i in range(1, len(stitched))
    ]
    assert agg_sharpe == pytest.approx(sharpe_ratio(rets))
    assert agg_maxdd == pytest.approx(max_drawdown(stitched))
    assert agg_maxdd < 0
    # WalkForwardReport must expose aggregate_oos_maxdd
    report = WalkForwardReport(
        steps=tuple(steps),
        aggregate_oos_return=agg_return,
        aggregate_oos_sharpe=agg_sharpe,
        aggregate_oos_maxdd=agg_maxdd,
        total_steps=3,
        successful_steps=successful,
    )
    assert report.aggregate_oos_maxdd == pytest.approx(agg_maxdd)
