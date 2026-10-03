"""Tests for walk-forward analysis engine."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from tradex_analytics.reports import sharpe_ratio
from tradex_domain import OHLC, Candle, Price, Quantity, Timeframe
from tradex_domain.instruments import Equity
from tradex_replay import walk_forward as wf
from tradex_replay.backtest import BacktestResult
from tradex_replay.optimization import make_grid_optimizer
from tradex_replay.walk_forward import (
    WalkForwardFoldError,
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
        """A raising optimizer fails the report, naming the fold and the error.

        It used to be swallowed: the fold was dropped, the run carried on, and
        the report was smaller and easier to pass. A fold that cannot be
        optimized was never a fold, and a report that quietly omits it is a
        promotion decision resting on less evidence than it claims.
        """
        data = list(range(400))

        def run_fn(params, subset):
            return _make_bt(total_return=0.01)

        def optimize_fn(train_data):
            raise RuntimeError("optimize failed")

        with pytest.raises(WalkForwardFoldError) as excinfo:
            run_walk_forward(
                data=data,
                run_fn=run_fn,
                optimize_fn=optimize_fn,
                train_window=252,
                test_window=63,
            )
        assert excinfo.value.step_index == 0
        assert "optimize failed" in excinfo.value.reason
        assert "RuntimeError" in excinfo.value.reason

    def test_run_fn_failure_handled(self):
        """Same for the run itself, in sample and out of sample alike."""
        data = list(range(400))

        def run_fn(params, subset):
            raise RuntimeError("run failed")

        def optimize_fn(train_data):
            return {"p": 1}

        with pytest.raises(WalkForwardFoldError) as excinfo:
            run_walk_forward(
                data=data,
                run_fn=run_fn,
                optimize_fn=optimize_fn,
                train_window=252,
                test_window=63,
            )
        assert excinfo.value.step_index == 0
        assert "run failed" in excinfo.value.reason

    def test_custom_score_fn(self):
        """``score_fn`` ranks the folds; it is not the source of the aggregate.

        This used to assert ``aggregate_oos_sharpe == 1.5``, the per run
        ``sharpe`` attribute read straight through — which is the number the
        whole research-validity lane exists to stop anyone reading. The aggregate
        is the Sharpe of the *stitched* out of sample series, so a run whose per
        fold Sharpe says 2.0 while its curve says otherwise reports the curve.

        The expected value is written out rather than read back: two folds
        returning 10% and 20% chain to ``[1.0, 1.1, 1.32]``, whose returns are
        exactly ``[0.1, 0.2]``.
        """
        per_fold_return = [0.10, 0.20]
        per_fold_sharpe = [3.0, 1.0]  # mean 2.0 — the number that must not appear
        calls: list[int] = []

        def run_fn(params, subset):
            fold = len(calls) // 2
            calls.append(1)
            return _make_bt(total_return=per_fold_return[fold], sharpe=per_fold_sharpe[fold])

        report = run_walk_forward(
            data=list(range(400)),
            run_fn=run_fn,
            optimize_fn=lambda train_data: {"p": 1},
            train_window=252,
            test_window=63,
            score_fn=lambda r: r.sharpe,
        )
        assert len(report.steps) == 2
        assert sum(per_fold_sharpe) / 2 == 2.0
        assert report.aggregate_oos_sharpe != pytest.approx(2.0)
        assert report.aggregate_oos_sharpe == pytest.approx(
            sharpe_ratio([0.1, 0.2], frequency=report.bar_frequency), rel=1e-9
        )

    def test_score_fn_is_the_folds_own_score_not_the_default(self):
        """``score_fn`` is wired to the fold, not accepted and ignored.

        The default scores a result by its total return, so a run whose Sharpe
        and return disagree is the only way to tell the two apart: the recorded
        ``in_sample_score`` / ``out_of_sample_score`` are 0.20 under the default
        and 0.50 under ``score_fn``, for one and the same run. The old suite had
        no such test — it asserted an aggregate number that never came from
        ``score_fn`` in the first place.
        """
        def run_fn(params, subset):
            return _make_bt(total_return=0.20, sharpe=0.50)

        def scores(score_fn):
            report = run_walk_forward(
                data=list(range(400)),
                run_fn=run_fn,
                optimize_fn=lambda train_data: {"p": 1},
                train_window=252,
                test_window=63,
                **({} if score_fn is None else {"score_fn": score_fn}),
            )
            step = report.steps[0]
            return step.in_sample_score, step.out_of_sample_score

        assert scores(None) == pytest.approx((0.20, 0.20))
        assert scores(lambda r: r.sharpe) == pytest.approx((0.50, 0.50))

    def test_grid_optimizer_composition(self):
        """make_grid_optimizer plugs into run_walk_forward as optimize_fn."""
        data = list(range(400))

        def run_fn(params, subset):
            return _make_bt(
                total_return=params.get("k", 1) * 0.01,
                sharpe=params.get("k", 1) * 1.0,
            )

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

    def test_basic(self, monkeypatch):
        """The date-window step arithmetic: 10 dates, 4 train + 2 test, stride 2.

        The windows here are far too small to be *valid* — 21 bars of warmup
        against a 4 day training window, and 3 scored out of sample bars against
        a floor of 30. Both are stood down deliberately, because this test is
        about which dates land in which window and nothing else; a window small
        enough to count is a separate claim, made in
        ``test_walk_forward_validity``. See
        ``test_a_fold_below_thirty_oos_bars_fails_the_report`` for the floor.
        """
        monkeypatch.setattr(wf, "require_min_oos_bars", lambda n_bars, **kw: n_bars)
        candles = [
            _candle(datetime(2026, 1, 1) + timedelta(days=day))
            for day in range(10)
        ]
        report = run_walk_forward_by_date(
            candles, lambda p, d: _make_bt(total_return=0.01), lambda d: {},
            train_days=4, test_days=2, warmup_bars=0,
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
            train_days=4, test_days=2, warmup_bars=0,
        )
        assert report.total_steps == 0

    def test_windows_span_all_symbols(self, monkeypatch):
        """Interleaved multi-symbol data: each window contains both symbols —
        the property that makes portfolio walk-forward meaningful.

        The two validity floors are stood down for the same reason as in
        ``test_basic``: 2 days x 2 symbols is 4 bars in a test window, which is a
        statement about window *arithmetic* and not a run anyone would ship.
        """
        monkeypatch.setattr(wf, "require_min_oos_bars", lambda n_bars, **kw: n_bars)
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
            train_days=4, test_days=2, warmup_bars=0,
        )
        assert report.total_steps == 2
        assert {str(i) for i in seen} == {"NSE:RELIANCE", "NSE:TCS"}


def test_walk_forward_stitched_oos_curve():
    """C2: the aggregate is read off the stitched series, and MaxDD exists.

    Two things changed shape since this was written and both were the point of
    the test. The segments now come from ``WalkForwardStep.oos_equity_curve`` —
    the fold's *scored* window, warmup excluded — rather than from the result's
    whole curve, which had the warmup in front of it. And ``_aggregate`` takes the
    bar frequency, because the Sharpe it reports is annualised and a Sharpe
    without its annualisation factor is not a number.
    """
    from tradex_analytics.reports import max_drawdown
    from tradex_replay.walk_forward import _aggregate

    seg1 = [1.0, 1.10]
    seg2 = [1.0, 1.20]
    seg3 = [1.0, 0.90]

    def _step(i: int, curve: list[float], total_return: float) -> WalkForwardStep:
        return WalkForwardStep(
            step_index=i, train_start=i * 10, train_end=i * 10 + 10,
            test_start=i * 10 + 10, test_end=i * 10 + 11,
            out_of_sample_result=_make_bt(total_return=total_return, sharpe=0.0),
            oos_equity_curve=curve,
            oos_bar_count=len(curve) - 1,
        )

    steps = [
        _step(0, seg1, 0.10),
        _step(1, seg2, 0.20),
        _step(2, seg3, -0.10),
    ]
    agg_return, agg_sharpe, agg_maxdd = _aggregate(steps, "daily")
    # old mean return
    mean_return = (0.10 + 0.20 - 0.10) / 3
    assert agg_return != pytest.approx(mean_return), "stitched return must differ from mean"
    # stitched: 1.0 ->1.10 ->1.32 ->1.188, so 0.188 on the first point
    assert agg_return == pytest.approx(0.188, rel=1e-6)
    # sharpe must be from the stitched returns, not the mean of the fold Sharpes
    stitched = [1.0, 1.10, 1.32, 1.188]
    rets = [
        (stitched[i] - stitched[i - 1]) / stitched[i - 1]
        for i in range(1, len(stitched))
    ]
    assert agg_sharpe == pytest.approx(sharpe_ratio(rets, frequency="daily"))
    assert agg_maxdd == pytest.approx(max_drawdown(stitched))
    # (1.188 - 1.32) / 1.32 = -0.1 exactly, so the deepest drawdown on the
    # stitched series is a tenth and not something read off a per fold number.
    assert agg_maxdd == pytest.approx(-0.1, rel=1e-9)
    assert agg_maxdd < 0
    # WalkForwardReport must expose aggregate_oos_maxdd
    report = WalkForwardReport(
        steps=tuple(steps),
        aggregate_oos_return=agg_return,
        aggregate_oos_sharpe=agg_sharpe,
        aggregate_oos_maxdd=agg_maxdd,
        total_steps=3,
        successful_steps=3,
    )
    assert report.aggregate_oos_maxdd == pytest.approx(agg_maxdd)


def test_the_curve_less_fallback_reports_zero_drawdown_and_says_why():
    """One observation per fold is a series, but not a drawdown measurement.

    A result carrying no equity curve at all is chained from its total return,
    so a Sharpe exists and is honest about being coarse. A *drawdown* off one
    point per fold would be a number that means nothing, so the fallback reports
    0.0 — and a caller that set a floor on ``aggregate_oos_maxdd`` is not being
    told the strategy had none.
    """
    from tradex_replay.walk_forward import _aggregate

    steps = [
        WalkForwardStep(
            step_index=i, train_start=0, train_end=10, test_start=10, test_end=11,
            out_of_sample_result=_make_bt(total_return=r, sharpe=s),
        )
        for i, (r, s) in enumerate([(0.10, 3.0), (0.20, 1.0)])
    ]
    agg_return, agg_sharpe, agg_maxdd = _aggregate(steps, "daily")
    # chained: 1.0 -> 1.1 -> 1.32, so returns are exactly [0.1, 0.2]
    assert agg_return == pytest.approx(0.32, rel=1e-9)
    assert agg_sharpe == pytest.approx(
        sharpe_ratio([0.1, 0.2], frequency="daily"), rel=1e-9
    )
    # The mean of the fold Sharpes is 2.0 and must not appear.
    assert agg_sharpe != pytest.approx(2.0)
    assert agg_maxdd == 0.0
