"""Walk-forward analysis engine.

Provides out-of-sample walk-forward testing with configurable
train/test windows and strategy re-optimization at each step.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

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
) -> WalkForwardReport:
    """Run walk-forward analysis.

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

    Returns
    -------
    WalkForwardReport
        Complete walk-forward analysis report.
    """
    if len(data) < train_window + test_window:
        raise ValueError(
            f"Need at least {train_window + test_window} data points, got {len(data)}"
        )

    _step = step_size or test_window
    steps: list[WalkForwardStep] = []
    step_idx = 0
    start = 0

    while start + train_window + test_window <= len(data):
        train_end = start + train_window
        test_end = min(train_end + test_window, len(data))

        train_data = data[start:train_end]
        test_data = data[train_end:test_end]

        # Optimize on training data
        try:
            best_params = optimize_fn(train_data)
        except Exception:
            best_params = {}

        # Run in-sample
        in_sample = None
        in_score = 0.0
        try:
            in_sample = run_fn(best_params, train_data)
            in_score = score_fn(in_sample) if score_fn else in_sample.total_return
        except Exception:
            pass

        # Run out-of-sample
        oos = None
        oos_score = 0.0
        try:
            oos = run_fn(best_params, test_data)
            oos_score = score_fn(oos) if score_fn else oos.total_return
        except Exception:
            pass

        steps.append(WalkForwardStep(
            step_index=step_idx,
            train_start=start,
            train_end=train_end,
            test_start=train_end,
            test_end=test_end,
            best_params=best_params,
            in_sample_result=in_sample,
            out_of_sample_result=oos,
            in_sample_score=in_score,
            out_of_sample_score=oos_score,
        ))

        step_idx += 1
        start += _step

    # Aggregate
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

    return WalkForwardReport(
        steps=tuple(steps),
        aggregate_oos_return=agg_return,
        aggregate_oos_sharpe=agg_sharpe,
        total_steps=len(steps),
        successful_steps=successful,
    )


__all__ = ["WalkForwardReport", "WalkForwardStep", "run_walk_forward"]
