"""Wave 1 lane 1d — walk forward validity (AC-3).

Each test here names one defect the lane set out to remove. The red state of
each is recorded in ``.superpowers/sdd/task-1d-report.md``.
"""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from tradex_analytics.reports import (
    TRADING_DAYS_PER_YEAR,
    _periods_per_year,
    max_drawdown,
    sharpe_ratio,
)
from tradex_analytics.research_validity import (
    MIN_OOS_BARS_PER_FOLD as SHARED_MIN_OOS_BARS_PER_FOLD,
)
from tradex_analytics.research_validity import InsufficientSampleError, require_min_oos_bars
from tradex_domain import OHLC, Candle, Price, Quantity, Timeframe
from tradex_domain.instruments import Equity
from tradex_replay import walk_forward as wf
from tradex_replay.backtest import BacktestResult
from tradex_replay.walk_forward import (
    DEFAULT_WARMUP_BARS,
    MIN_OOS_BARS_PER_FOLD,
    WalkForwardFoldError,
    run_walk_forward,
    run_walk_forward_by_date,
)

# --------------------------------------------------------------------------- fixtures


def _candle(
    ts: datetime,
    timeframe: Timeframe = Timeframe.M1,
    symbol: str = "RELIANCE",
) -> Candle:
    price = Price(value=Decimal("100"))
    return Candle(
        instrument=Equity.of("NSE", symbol),
        timeframe=timeframe,
        ohlc=OHLC(open=price, high=price, low=price, close=price),
        volume=Quantity(value=Decimal("1000")),
        timestamp=ts,
    )


def _bt(total_return: float = 0.0, sharpe: float = 0.0, curve=None) -> BacktestResult:
    return BacktestResult(
        total_return=total_return,
        sharpe=sharpe,
        max_drawdown=0.0,
        num_trades=0,
        equity_curve=list(curve) if curve is not None else [],
    )


#: A deterministic, non-constant return pattern.  A constant one has zero
#: variance, so every Sharpe taken against it would be 0.0 and prove nothing.
_PATTERN = (0.004, -0.002, 0.001, 0.003, -0.001, 0.002, 0.0005, -0.003)


def _curve(n: int, rotation: int = 0) -> list[float]:
    """A bar-aligned equity curve: one leading point, then one per bar.

    The backtest engine seeds the curve with the initial capital and appends one
    mark-to-market point per candle, so ``len(curve) == bars + 1``.
    """
    out = [1.0]
    for i in range(n):
        out.append(out[-1] * (1 + _PATTERN[(i + rotation) % len(_PATTERN)]))
    return out


def _returns(curve: list[float]) -> list[float]:
    return [
        (curve[i] - curve[i - 1]) / curve[i - 1]
        for i in range(1, len(curve))
        if curve[i - 1]
    ]


def _stitch(segments: list[list[float]]) -> list[float]:
    """Independent re-implementation of the module's segment chaining."""
    stitched = [segments[0][0]]
    for seg in segments:
        base = seg[0] or 1.0
        last = stitched[-1]
        stitched.extend((x / base) * last for x in seg[1:])
    return stitched


#: Two folds, 200 training bars, 100 test bars: the shape most tests below use.
_DATA = list(range(400))


def _two_folds():
    return run_walk_forward(
        _DATA, lambda p, d: _bt(curve=_curve(len(d))), lambda d: {},
        train_window=200, test_window=100, step_size=100,
    )


# --------------------------------------------------------------------------- 1. warmup


def test_warmup_bar_count_is_max_indicator_period_plus_one():
    """21 = the longest lookback in the shipped default parameter set (20) + 1.

    ``sma_cross`` slow, ``bollinger_breakout`` length and ``bracket_breakout``
    lookback all default to 20, and an indicator needs ``period + 1`` closes
    before it emits its first value.
    """
    assert DEFAULT_WARMUP_BARS == 21


def test_oos_window_starts_warm_from_the_train_tail():
    """Exact indices: warmup is the *tail* of the training window, the embargo
    sits between it and the first scored bar, and the scored run is the rest.

    A 200 bar training window ends at 200, so the warmup is bars 179..199, the
    embargo bar is 200, and the scored out of sample bars are 201..262.
    """
    data = list(range(263))
    calls: list[list[int]] = []

    def run_fn(params, subset):
        calls.append(list(subset))
        return _bt(curve=_curve(len(subset)))

    report = run_walk_forward(
        data, run_fn, lambda d: {}, train_window=200, test_window=63
    )

    assert report.total_steps == 1
    assert len(calls) == 2, "one in-sample run and one out-of-sample run per fold"
    assert calls[0] == data[0:200], "in-sample is the untouched training window"

    oos_input = calls[1]
    warmup = oos_input[:DEFAULT_WARMUP_BARS]
    assert warmup == data[179:200], "warmup is the training tail, not its head"
    assert oos_input[DEFAULT_WARMUP_BARS] == 200, "the embargo bar follows the tail"
    assert oos_input[22:] == data[201:263], "scored OOS bars start after the embargo"
    assert report.steps[0].warmup_bars == 21
    assert report.steps[0].embargo_bars == 1
    assert report.steps[0].oos_bar_count == 62


def test_a_fold_that_cannot_supply_the_warmup_fails_the_report():
    """A 20 bar training window cannot carry 21 bars. It must not start cold."""
    with pytest.raises(WalkForwardFoldError) as excinfo:
        run_walk_forward(
            list(range(4000)),
            lambda p, d: _bt(),
            lambda d: {},
            train_window=20,
            test_window=100,
        )
    message = str(excinfo.value)
    assert "fold 0" in message
    assert "21" in message and "20" in message


def test_warmup_bars_are_not_counted_as_out_of_sample():
    """The warmup tail is training data; it may not enter the OOS equity segment.

    ``run_fn`` returns a curve over every bar it was handed, warmup included.
    If the aggregate were taken from the whole curve, training window P&L would
    be reported as out of sample.
    """
    data = list(range(263))

    def run_fn(params, subset):
        return _bt(curve=_curve(len(subset)))

    report = run_walk_forward(
        data, run_fn, lambda d: {}, train_window=200, test_window=63
    )
    step = report.steps[0]
    assert step.oos_bar_count == 62
    assert len(step.oos_equity_curve) == 63, "one leading point plus 62 scored bars"

    full = _curve(84)
    assert report.aggregate_oos_return != pytest.approx((full[-1] - full[0]) / full[0])
    assert report.aggregate_oos_maxdd == pytest.approx(max_drawdown(step.oos_equity_curve))


# --------------------------------------------------------------------------- 2. embargo


@pytest.mark.parametrize(
    ("window_length_bars", "expected"),
    [
        (1, 1),      # the max(1, ...) floor
        (63, 1),     # ceil(0.63)
        (100, 1),    # ceil(1.00)
        (101, 2),    # ceil(1.01)
        (250, 3),    # ceil(2.50)
        (1000, 10),  # ceil(10.00)
    ],
)
def test_embargo_is_one_percent_of_the_window_with_a_floor_of_one(
    window_length_bars: int, expected: int
):
    assert wf._embargo_bars(window_length_bars) == expected


def test_train_and_test_windows_do_not_touch():
    """There is a real hole between the last training bar and the first scored bar.

    A 1000 bar test window embargoes 10 bars, so the run receives 21 warmup
    training-tail bars, then 10 embargo bars, then 990 scored bars, and the
    scored count is ``test_window - embargo``.
    """
    data = list(range(1263))
    calls: list[list[int]] = []

    def run_fn(params, subset):
        calls.append(list(subset))
        return _bt(curve=_curve(len(subset)))

    report = run_walk_forward(
        data, run_fn, lambda d: {},
        train_window=200, test_window=1000, step_size=1000,
    )
    assert report.total_steps == 1
    step = report.steps[0]
    assert step.embargo_bars == 10, "ceil(0.01 * 1000)"
    assert step.warmup_bars == 21
    assert step.oos_bar_count == 1000 - 10 == 990
    assert calls[1] == data[179:200] + data[200:210] + data[210:1200]
    assert step.train_end == 200
    assert step.test_start == 200, "the window handed over starts at the boundary"
    # The hole, position by position: last training bar, then the embargo gap,
    # then the first scored bar.
    assert calls[1][20] == 199
    assert calls[1][21:31] == list(range(200, 210))
    assert calls[1][31] == 210
    assert calls[1][-1] == data[1199]


# --------------------------------------------------------------------------- 3. frequency


def test_aggregate_annualises_at_the_stored_bar_frequency():
    """1m data annualises at 94,500 periods a year, not at 252."""
    candles = [
        _candle(datetime(2026, 1, 5, 9, 15) + timedelta(minutes=i))
        for i in range(2000)
    ]
    report = run_walk_forward(
        candles, lambda p, d: _bt(curve=_curve(len(d))), lambda d: {},
        train_window=200, test_window=63,
    )
    assert report.bar_frequency == "1m"
    assert _periods_per_year("1m") == TRADING_DAYS_PER_YEAR * 375 == 94_500

    expected = sharpe_ratio(
        _returns(_stitch([s.oos_equity_curve for s in report.steps])),
        frequency="1m",
    )
    assert report.aggregate_oos_sharpe == pytest.approx(expected, rel=1e-9)

    daily = sharpe_ratio(
        _returns(_stitch([s.oos_equity_curve for s in report.steps])),
        frequency="daily",
    )
    assert report.aggregate_oos_sharpe != pytest.approx(daily, rel=1e-6)
    # With a zero risk-free rate the Sharpe scales with sqrt(periods), so the
    # ratio is exactly sqrt(375).  This is the whole defect in one assertion:
    # the old code reported the daily number for 1m bars.
    assert report.aggregate_oos_sharpe / daily == pytest.approx(375**0.5, rel=1e-9)


def test_daily_candles_annualise_at_252():
    candles = [
        _candle(datetime(2026, 1, 5) + timedelta(days=i), timeframe=Timeframe.D1)
        for i in range(400)
    ]
    report = run_walk_forward(
        candles, lambda p, d: _bt(curve=_curve(len(d))), lambda d: {},
        train_window=200, test_window=63,
    )
    assert report.bar_frequency == "1d"
    assert _periods_per_year(report.bar_frequency) == TRADING_DAYS_PER_YEAR


def test_mixed_bar_frequencies_fail_before_the_run():
    """AC-3 asserts the frequency is constant *before* the run, not during it."""
    calls: list[int] = []
    candles = [
        _candle(datetime(2026, 1, 5) + timedelta(days=i)) for i in range(150)
    ] + [
        _candle(datetime(2026, 2, 5) + timedelta(days=i), timeframe=Timeframe.D1)
        for i in range(150)
    ]

    def run_fn(params, subset):
        calls.append(1)
        return _bt()

    with pytest.raises(ValueError) as excinfo:
        run_walk_forward(
            candles, run_fn, lambda d: {},
            train_window=200, test_window=63,
        )
    message = str(excinfo.value)
    assert "1m" in message and "1d" in message, message
    assert calls == [], "no fold may run once the frequency is known to vary"


# --------------------------------------------------------------------------- 3b. no mean of Sharpes


def test_aggregate_sharpe_is_the_concatenated_series_not_the_fold_sharpes():
    """Regression guard on promotion criterion 1: concatenated, never averaged."""
    per_fold = [3.0, 1.0]
    calls: list[int] = []

    def run_fn(params, subset):
        fold = len(calls) // 2
        calls.append(1)
        return _bt(sharpe=per_fold[fold], curve=_curve(len(subset), rotation=fold))

    report = run_walk_forward(
        _DATA, run_fn, lambda d: {},
        train_window=200, test_window=100, step_size=100,
    )
    assert len(report.steps) == 2
    assert sum(per_fold) / 2 == 2.0
    assert report.aggregate_oos_sharpe != pytest.approx(2.0)
    assert report.aggregate_oos_sharpe == pytest.approx(
        sharpe_ratio(
            _returns(_stitch([s.oos_equity_curve for s in report.steps])),
            frequency=report.bar_frequency,
        ),
        rel=1e-9,
    )


def test_the_curve_less_fallback_also_never_averages_fold_sharpes():
    """RED: the old fallback reported exactly the mean of the per fold Sharpes."""
    per_fold = [3.0, 1.0]
    fold_returns = [0.10, -0.05]
    calls: list[int] = []

    def run_fn(params, subset):
        fold = len(calls) // 2
        calls.append(1)
        return _bt(total_return=fold_returns[fold], sharpe=per_fold[fold])

    report = run_walk_forward(
        _DATA, run_fn, lambda d: {},
        train_window=200, test_window=100, step_size=100,
    )
    assert sum(per_fold) / 2 == 2.0
    assert report.aggregate_oos_sharpe != pytest.approx(2.0)
    chained = [1.0, 1.0 * 1.10, 1.0 * 1.10 * 0.95]
    assert report.aggregate_oos_sharpe == pytest.approx(
        sharpe_ratio(_returns(chained), frequency=report.bar_frequency), rel=1e-9
    )


# --------------------------------------------------------------------------- 4. raising folds


def test_a_raising_fold_fails_the_whole_report_naming_the_fold_and_the_exception():
    """The defect: a raising fold became a zero score and the report carried on.

    Dropping fold 1 would leave a report with one fold, a shorter out of sample
    span and fewer trades — every promotion criterion easier.  So the run must
    raise, naming fold 1 and the exception.
    """
    calls: list[int] = []

    def run_fn(params, subset):
        if len(calls) >= 3:  # fold 0 runs twice, fold 1 raises on its OOS call
            raise ValueError("sentinel-boom")
        calls.append(1)
        return _bt(curve=_curve(len(subset)))

    with pytest.raises(WalkForwardFoldError) as excinfo:
        run_walk_forward(
            _DATA, run_fn, lambda d: {},
            train_window=200, test_window=100, step_size=100,
        )
    message = str(excinfo.value)
    assert "fold 1" in message, message
    assert "sentinel-boom" in message, message
    assert isinstance(excinfo.value.__cause__, ValueError)


def _always_raises(body: list[ast.stmt]) -> bool:
    """True when every path through *body* ends in a ``raise``."""
    if not body:
        return False
    for node in body:
        if isinstance(node, ast.Raise):
            continue
        if (
            isinstance(node, ast.If)
            and node.orelse
            and _always_raises(node.body)
            and _always_raises(node.orelse)
        ):
            continue
        return False
    return True


def test_a_raising_fold_cannot_be_dropped_into_a_zero_score():
    """Negative control: the code path that did it is gone, not merely unused.

    The three handlers this module used to carry assigned ``best_params = {}``,
    ``in_sample = None`` and ``out_of_sample_score = 0.0`` and returned.  Every
    handler left re-raises, and none assigns anything, so there is no longer a
    place where a raising fold can become a number.
    """
    tree = ast.parse(inspect.getsource(wf))
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
    assert handlers, "the fold wrapper is expected to re-raise as WalkForwardFoldError"
    for handler in handlers:
        label = ast.unparse(handler.type) if handler.type else "bare except"
        assert _always_raises(handler.body), f"{label} does not re-raise"
        assigned = [n for n in ast.walk(handler) if isinstance(n, ast.Assign)]
        assert not assigned, f"{label} assigns {ast.unparse(assigned[0])}"


def test_a_raising_optimizer_fails_the_report():
    """A crashing grid cell dropped from the argmax is the overfitting trap.

    ``optimize_fn`` used to swallow and hand back empty params, so the grid
    search's survivors decided the winner.
    """

    def optimize_fn(train_data):
        raise RuntimeError("grid-search-diverged")

    with pytest.raises(WalkForwardFoldError) as excinfo:
        run_walk_forward(
            list(range(4000)), lambda p, d: _bt(), optimize_fn,
            train_window=200, test_window=100,
        )
    assert "grid-search-diverged" in str(excinfo.value)
    assert "fold 0" in str(excinfo.value)


def test_an_in_sample_failure_also_fails_the_report():
    def run_fn(params, subset):
        if len(subset) == 200:
            raise RuntimeError("in-sample-boom")
        return _bt(curve=_curve(len(subset)))

    with pytest.raises(WalkForwardFoldError) as excinfo:
        run_walk_forward(
            list(range(4000)), run_fn, lambda d: {},
            train_window=200, test_window=100,
        )
    assert "in-sample-boom" in str(excinfo.value)


# --------------------------------------------------------------------------- 5. floor and span


def test_minimum_out_of_sample_bars_per_fold_is_thirty():
    assert MIN_OOS_BARS_PER_FOLD == 30


def test_a_fold_below_thirty_oos_bars_fails_the_report():
    """29 scored bars cannot support an annualised Sharpe. It must not pass."""
    with pytest.raises(WalkForwardFoldError) as excinfo:
        run_walk_forward(
            list(range(1000)), lambda p, d: _bt(), lambda d: {},
            train_window=200, test_window=30, step_size=30,
        )
    message = str(excinfo.value)
    assert "fold 0" in message
    assert "29" in message


def test_a_fold_below_thirty_raises_through_the_shared_guard():
    """The fold is stopped by the one guard, not by a second copy of the floor.

    ``require_min_oos_bars`` is what ``grid_search`` uses, and it used to be
    reachable from nowhere: the walk forward fold compared the count against a
    literal of its own, so a floor could be raised, relaxed or deleted on
    either side without either side noticing. The cause is asserted rather
    than the message, because the cause is the guard.
    """
    with pytest.raises(WalkForwardFoldError) as excinfo:
        run_walk_forward(
            list(range(1000)), lambda p, d: _bt(), lambda d: {},
            train_window=200, test_window=30, step_size=30,
        )
    assert isinstance(excinfo.value.__cause__, InsufficientSampleError)
    assert "29" in str(excinfo.value.__cause__)
    assert str(MIN_OOS_BARS_PER_FOLD) in str(excinfo.value.__cause__)


def test_the_walk_forward_floor_is_the_shared_floor_not_a_second_declaration():
    """One number, declared once, in ``analytics``.

    The module re-exports it for the fold's own callers, so nothing here binds
    the name: a second ``MIN_OOS_BARS_PER_FOLD = 30`` in this file would be a
    floor the grid search does not read.
    """
    bound = {
        target.id
        for node in ast.walk(ast.parse(inspect.getsource(wf)))
        if isinstance(node, ast.AnnAssign | ast.Assign)
        for target in ([node.target] if isinstance(node, ast.AnnAssign) else node.targets)
        if isinstance(target, ast.Name)
    }
    assert "MIN_OOS_BARS_PER_FOLD" not in bound, "a second floor is bound here"
    # Re-exported, so it is the *same* floor the grid search enforces.
    assert wf.MIN_OOS_BARS_PER_FOLD == SHARED_MIN_OOS_BARS_PER_FOLD
    assert wf.require_min_oos_bars is require_min_oos_bars


def test_the_fold_guard_is_armed_by_a_computed_count(monkeypatch):
    """The wiring: each fold hands the guard its *own* scored bar count.

    100 bar test windows, one bar of embargo, so 99 scored — a number only the
    fold can compute, and off by the embargo if the fold passed the window it
    was given instead of what it scored.
    """
    declared: list[tuple[int, dict]] = []
    shared = wf.require_min_oos_bars

    def spy(n_bars: int, **kwargs) -> int:
        declared.append((n_bars, kwargs))
        return shared(n_bars, **kwargs)

    monkeypatch.setattr(wf, "require_min_oos_bars", spy)
    report = run_walk_forward(
        list(range(400)), lambda p, d: _bt(curve=_curve(len(d))), lambda d: {},
        train_window=200, test_window=100, step_size=100,
    )
    assert [n for n, _ in declared] == [99, 99]  # one declaration per fold
    assert [s.oos_bar_count for s in report.steps] == [99, 99]


def test_withholding_the_declared_count_disarms_the_fold_guard(monkeypatch):
    """Negative control: the *declaration* is what arms the floor.

    The fold reaches the guard through the module global, so substituting a
    guard that is never told a count is what removing the argument from the
    fold call amounts to — and the 29 bar fold is then reported as a result.
    That is the exact failure this item exists to close: a floor that is
    present, tested, exported, and never fires.
    """
    monkeypatch.setattr(wf, "require_min_oos_bars", lambda n_bars, **kw: n_bars)
    report = run_walk_forward(
        list(range(1000)), lambda p, d: _bt(curve=_curve(len(d))), lambda d: {},
        train_window=200, test_window=30, step_size=30,
    )
    assert report.steps[0].oos_bar_count == 29  # the hole the floor exists for
    assert report.total_steps > 0


def test_exactly_thirty_oos_bars_is_accepted():
    """The floor is 30 and it is inclusive: 30 passes, 29 does not."""
    report = run_walk_forward(
        list(range(1000)), lambda p, d: _bt(), lambda d: {},
        train_window=200, test_window=31, step_size=31,  # 31 - embargo(1) = 30
    )
    assert report.steps[0].oos_bar_count == MIN_OOS_BARS_PER_FOLD
    assert report.total_steps > 0


def test_oos_span_days_is_the_calendar_span_of_the_out_of_sample_bars():
    """First to last out of sample bar, in calendar days, across every fold.

    Under purged walk forward the span legitimately contains training windows
    and embargo gaps, so it is a span and not a claim of continuous out of
    sample data — which is why it exceeds the sum of the scored bars.
    """
    bars_per_day = 12
    days = 20
    candles = [
        _candle(
            datetime(2026, 1, 5, 9, 15) + timedelta(days=day, minutes=minute),
            timeframe=Timeframe.D1,
        )
        for day in range(days)
        for minute in range(bars_per_day)
    ]
    report = run_walk_forward_by_date(
        candles, lambda p, d: _bt(), lambda d: {}, train_days=4, test_days=3,
    )
    assert report.total_steps == 5
    assert report.steps[0].oos_bar_count == 3 * bars_per_day - 1  # 36 - embargo(1)

    first = datetime(2026, 1, 9, 9, 16)   # day 4, first bar after the embargo
    last = datetime(2026, 1, 23, 9, 26)   # day 18, last bar of the last fold
    span = (last - first).total_seconds() / 86_400.0
    assert report.oos_span_days == pytest.approx(span)
    assert (last - first).days == 14
    assert 14 < report.oos_span_days < 15
    # A span, not a bar count: one fold's out of sample covers 3 days, so the
    # 14 day span is evidence it reaches across the four training windows and
    # embargo gaps between them.
    assert report.steps[0].oos_bar_count == 35
    assert report.oos_span_days > 3 * 86_400 / 86_400


def test_span_is_zero_when_the_bars_carry_no_timestamp():
    """Index mode over a plain sequence cannot place a span, and says 0."""
    assert _two_folds().oos_span_days == 0.0
