"""Walk-forward analysis engine.

Provides out-of-sample walk-forward testing with configurable
train/test windows and strategy re-optimization at each step.

Two windowing modes:

- :func:`run_walk_forward` — index-based windows over a single series.
- :func:`run_walk_forward_by_date` — calendar windows over (possibly
  multi-symbol) data, so each train/test window contains every instrument's
  bars for the covered dates.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from tradex_trading.analytics.reports import max_drawdown, sharpe_ratio
from tradex_trading.replay.backtest import BacktestResult


@dataclass(frozen=True, slots=True)
class WalkForwardStep:
    """One step in walk-forward analysis."""

    step_index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    best_params: dict[str, Any] = field(default_factory=dict)
    in_sample_result: BacktestResult | None = None
    out_of_sample_result: BacktestResult | None = None
    in_sample_score: float = 0.0
    out_of_sample_score: float = 0.0


@dataclass(frozen=True, slots=True)
class WalkForwardReport:
    """Complete walk-forward analysis report."""

    steps: tuple[WalkForwardStep, ...]
    aggregate_oos_return: float = 0.0
    aggregate_oos_sharpe: float = 0.0
    aggregate_oos_maxdd: float = 0.0
    total_steps: int = 0
    successful_steps: int = 0

    @property
    def success_rate(self) -> float:
        """Fraction of steps that completed successfully."""
        return self.successful_steps / self.total_steps if self.total_steps > 0 else 0.0

    def summary(self) -> str:
        """Human-readable summary."""
        lines = [
            "=" * 50,
            "WALK-FORWARD ANALYSIS REPORT",
            "=" * 50,
            f"Total Steps:          {self.total_steps}",
            f"Successful Steps:     {self.successful_steps}",
            f"Success Rate:         {self.success_rate:.1%}",
            f"Aggregate OOS Return: {self.aggregate_oos_return:.2%}",
            f"Aggregate OOS Sharpe: {self.aggregate_oos_sharpe:.2f}",
            "=" * 50,
        ]
        for step in self.steps:
            oos_ret = (
                getattr(step.out_of_sample_result, "total_return", 0.0)
                if step.out_of_sample_result
                else 0.0
            )
            lines.append(
                f"  Step {step.step_index}: "
                f"train=[{step.train_start}:{step.train_end}] "
                f"test=[{step.test_start}:{step.test_end}] "
                f"oos_return={oos_ret:.2%}"
            )
        return "\n".join(lines)


def run_walk_forward(
    data: Sequence[Any],
    run_fn: Callable[[dict[str, Any], Sequence[Any]], BacktestResult],
    optimize_fn: Callable[[Sequence[Any]], dict[str, Any]],
    *,
    train_window: int = 252,
    test_window: int = 63,
    step_size: int | None = None,
    score_fn: Callable[[BacktestResult], float] | None = None,
    purge_bars: int = 0,
) -> WalkForwardReport:
    """Run walk-forward analysis over index-based windows.

    Parameters
    ----------
    data : Sequence[Any]
        Full dataset (e.g., list of Candle).
    run_fn : Callable
        Function that takes (params, data_subset) and returns BacktestResult.
    optimize_fn : Callable
        Function that takes training data and returns best params.
    train_window : int
        Number of data points for training.
    test_window : int
        Number of data points for testing.
    step_size : int | None
        Step size between windows. Defaults to test_window.
    score_fn : Callable | None
        Scoring function. Defaults to total_return.
    purge_bars : int
        Bars to skip at the start of each OOS test window (warmup/overlap
        purge). 0 = no purge (compat default).

    Returns
    -------
    WalkForwardReport
        Complete walk-forward analysis report.

    Raises
    ------
    ValueError
        If ``len(data) < train_window + test_window``.
    """
    if len(data) < train_window + test_window:
        raise ValueError(
            f"Need at least {train_window + test_window} data points, got {len(data)}"
        )

    _step = step_size or test_window
    steps: list[WalkForwardStep] = []
    step_idx = 0
    start = 0

    # ponytail: carry warmup by seeding strategy._closes from train tail  # noqa: E501
    # when test_window < slow_period — add when test_window < 50  # noqa: E501
    while start + train_window + test_window <= len(data):
        train_end = start + train_window
        test_end = min(train_end + test_window, len(data))
        _test_data = data[train_end:test_end]
        if purge_bars:
            _test_data = _test_data[purge_bars:]
        steps.append(_evaluate_step(
            step_index=step_idx,
            train_start=start,
            train_end=train_end,
            test_start=train_end,
            test_end=test_end,
            train_data=data[start:train_end],
            test_data=_test_data,
            run_fn=run_fn,
            optimize_fn=optimize_fn,
            score_fn=score_fn,
        ))
        step_idx += 1
        start += _step

    agg_return, agg_sharpe, successful, agg_maxdd = _aggregate(steps)
    return WalkForwardReport(
        steps=tuple(steps),
        aggregate_oos_return=agg_return,
        aggregate_oos_sharpe=agg_sharpe,
        aggregate_oos_maxdd=agg_maxdd,
        total_steps=len(steps),
        successful_steps=successful,
    )


def run_walk_forward_by_date(
    data: Sequence[Any],
    run_fn: Callable[[dict[str, Any], Sequence[Any]], BacktestResult],
    optimize_fn: Callable[[Sequence[Any]], dict[str, Any]],
    *,
    train_days: int = 30,
    test_days: int = 15,
    score_fn: Callable[[BacktestResult], float] | None = None,
    purge_bars: int = 0,
) -> WalkForwardReport:
    """Run walk-forward analysis over calendar-date windows.

    Suitable for multi-symbol series where index-based windows would slice
    interleaved instruments into near-empty pieces: each window contains
    every instrument's bars for the covered dates.

    Parameters
    ----------
    data : Sequence[Any]
        Full dataset (e.g., list of Candle, possibly multi-symbol
        interleaved). Each item must expose a ``timestamp`` attribute.
    run_fn : Callable
        Function that takes (params, data_subset) and returns BacktestResult.
    optimize_fn : Callable
        Function that takes training data and returns best params.
    train_days : int
        Training window length in calendar days.
    test_days : int
        Testing window length in calendar days.
    score_fn : Callable | None
        Scoring function. Defaults to total_return.
    purge_bars : int
        Bars to skip at the start of each OOS test window (warmup purge).
        0 = no purge (compat default).

    Returns
    -------
    WalkForwardReport
        Complete walk-forward analysis report (step indices are day offsets
        into the sorted date list, not bar indices).

    Notes
    -----
    The step stride is exactly *test_days* (no overlap), unlike
    :func:`run_walk_forward` which accepts a custom ``step_size``.

    Raises
    ------
    ValueError
        If *data* is empty.
    """
    if not data:
        raise ValueError("run_walk_forward_by_date: data is empty")

    by_date: dict[date, list[Any]] = {}
    for item in data:
        by_date.setdefault(item.timestamp.date(), []).append(item)
    dates = sorted(by_date)

    steps: list[WalkForwardStep] = []
    step_idx = 0
    start = 0
    # ponytail: carry warmup by seeding strategy._closes from train tail  # noqa: E501
    # when test_window < slow_period — add when test_window < 50  # noqa: E501
    while start + train_days + test_days <= len(dates):
        train_dates = dates[start:start + train_days]
        test_dates = dates[start + train_days:start + train_days + test_days]
        _test_data = [c for d in test_dates for c in by_date[d]]
        if purge_bars:
            _test_data = _test_data[purge_bars:]
        steps.append(_evaluate_step(
            step_index=step_idx,
            train_start=start,
            train_end=start + train_days,
            test_start=start + train_days,
            test_end=start + train_days + test_days,
            train_data=[c for d in train_dates for c in by_date[d]],
            test_data=_test_data,
            run_fn=run_fn,
            optimize_fn=optimize_fn,
            score_fn=score_fn,
        ))
        step_idx += 1
        start += test_days

    agg_return, agg_sharpe, successful, agg_maxdd = _aggregate(steps)
    return WalkForwardReport(
        steps=tuple(steps),
        aggregate_oos_return=agg_return,
        aggregate_oos_sharpe=agg_sharpe,
        aggregate_oos_maxdd=agg_maxdd,
        total_steps=len(steps),
        successful_steps=successful,
    )


# --------------------------------------------------------------------------- helpers


def _evaluate_step(
    *,
    step_index: int,
    train_start: int,
    train_end: int,
    test_start: int,
    test_end: int,
    train_data: Sequence[Any],
    test_data: Sequence[Any],
    run_fn: Callable[[dict[str, Any], Sequence[Any]], BacktestResult],
    optimize_fn: Callable[[Sequence[Any]], dict[str, Any]],
    score_fn: Callable[[BacktestResult], float] | None,
) -> WalkForwardStep:
    """Optimize on the train window, then run in-sample and out-of-sample.

    Every failure mode degrades gracefully: a raising ``optimize_fn`` yields
    empty params; a raising ``run_fn`` yields a step with no result.
    """
    try:
        best_params = optimize_fn(train_data)
    except Exception:
        best_params = {}

    in_sample = None
    in_score = 0.0
    try:
        in_sample = run_fn(best_params, train_data)
        in_score = score_fn(in_sample) if score_fn else in_sample.total_return
    except Exception:
        pass

    oos = None
    oos_score = 0.0
    try:
        oos = run_fn(best_params, test_data)
        oos_score = score_fn(oos) if score_fn else oos.total_return
    except Exception:
        pass

    return WalkForwardStep(
        step_index=step_index,
        train_start=train_start,
        train_end=train_end,
        test_start=test_start,
        test_end=test_end,
        best_params=best_params,
        in_sample_result=in_sample,
        out_of_sample_result=oos,
        in_sample_score=in_score,
        out_of_sample_score=oos_score,
    )


def _aggregate(steps: Sequence[WalkForwardStep]) -> tuple[float, float, int, float]:
    """Stitched OOS equity curve — chain normalized segments multiplicatively."""
    segments = [
        s.out_of_sample_result.equity_curve
        for s in steps
        if s.out_of_sample_result is not None and s.out_of_sample_result.equity_curve
    ]
    if not segments:
        # fallback for legacy BacktestResult without equity_curve
        oos_returns = [
            s.out_of_sample_result.total_return
            for s in steps
            if s.out_of_sample_result is not None
        ]
        oos_sharpes = [
            getattr(s.out_of_sample_result, "sharpe", 0.0)
            for s in steps
            if s.out_of_sample_result is not None
        ]
        agg_return = sum(oos_returns) / len(oos_returns) if oos_returns else 0.0
        agg_sharpe = sum(oos_sharpes) / len(oos_sharpes) if oos_sharpes else 0.0
        successful = sum(1 for s in steps if s.out_of_sample_result is not None)
        return agg_return, agg_sharpe, successful, 0.0
    stitched: list[float] = [segments[0][0]]
    for seg in segments:
        base = seg[0] or 1.0
        norm = [x / base for x in seg[1:]]
        last = stitched[-1]
        stitched.extend(v * last for v in norm)
    returns = [
        (stitched[i] - stitched[i - 1]) / stitched[i - 1]
        for i in range(1, len(stitched))
        if stitched[i - 1] != 0
    ]
    agg_return = (stitched[-1] - stitched[0]) / stitched[0] if stitched[0] else 0.0
    agg_sharpe = sharpe_ratio(returns)
    agg_maxdd = max_drawdown(stitched)
    successful = sum(1 for s in steps if s.out_of_sample_result is not None)
    return agg_return, agg_sharpe, successful, agg_maxdd


__all__ = [
    "WalkForwardReport",
    "WalkForwardStep",
    "run_walk_forward",
    "run_walk_forward_by_date",
]
