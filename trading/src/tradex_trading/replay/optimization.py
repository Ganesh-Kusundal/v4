"""Parameter optimization for backtesting.

Provides grid search and walk-forward optimization over strategy parameters.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from tradex_trading.replay.backtest import BacktestResult


@dataclass(frozen=True, slots=True)
class OptimizationResult:
    """Result from a single parameter combination."""

    params: dict[str, Any]
    result: BacktestResult
    score: float = 0.0

    def __post_init__(self) -> None:
        # Default score is total_return
        if self.score == 0.0:
            object.__setattr__(self, "score", self.result.total_return)


@dataclass(frozen=True, slots=True)
class GridSearchResult:
    """Result from a grid search optimization."""

    results: tuple[OptimizationResult, ...]
    best: OptimizationResult | None = None
    total_combinations: int = 0

    @property
    def top_n(self) -> list[OptimizationResult]:
        """Return results sorted by score descending."""
        return sorted(self.results, key=lambda r: r.score, reverse=True)


def grid_search(
    param_grid: dict[str, Sequence[Any]],
    run_fn: Callable[[dict[str, Any]], BacktestResult],
    score_fn: Callable[[BacktestResult], float] | None = None,
) -> GridSearchResult:
    """Run a grid search over parameter combinations.

    Parameters
    ----------
    param_grid : dict[str, Sequence[Any]]
        Parameter names mapped to sequences of values to try.
        Example: {"fast_period": [5, 10, 20], "slow_period": [30, 50, 100]}
    run_fn : Callable
        Function that takes a param dict and returns a BacktestResult.
    score_fn : Callable | None
        Function to score a BacktestResult. Defaults to total_return.

    Returns
    -------
    GridSearchResult
        Results from all parameter combinations.

    Example
    -------
    >>> def run_backtest(params):
    ...     engine = BacktestEngine()
    ...     strategy = MyStrategy(**params)
    ...     return engine.run(strategy, data)
    >>> result = grid_search(
    ...     {"fast": [5, 10], "slow": [30, 50]},
    ...     run_backtest,
    ... )
    >>> print(result.best.params)
    """
    keys = list(param_grid.keys())
    values = list(param_grid.values())
    combinations = list(itertools.product(*values))

    results: list[OptimizationResult] = []
    for combo in combinations:
        params = dict(zip(keys, combo))
        try:
            bt_result = run_fn(params)
            score = score_fn(bt_result) if score_fn else bt_result.total_return
            results.append(OptimizationResult(params=params, result=bt_result, score=score))
        except Exception:
            continue

    best = max(results, key=lambda r: r.score) if results else None

    return GridSearchResult(
        results=tuple(results),
        best=best,
        total_combinations=len(combinations),
    )


@dataclass(frozen=True, slots=True)
class WalkForwardResult:
    """Result from walk-forward optimization."""

    in_sample_results: tuple[OptimizationResult, ...]
    out_of_sample_results: tuple[OptimizationResult, ...]
    best_params: dict[str, Any] = field(default_factory=dict)
    total_return: float = 0.0


def walk_forward(
    data: Sequence[Any],
    param_grid: dict[str, Sequence[Any]],
    run_fn: Callable[[dict[str, Any], Sequence[Any]], BacktestResult],
    n_splits: int = 5,
    score_fn: Callable[[BacktestResult], float] | None = None,
) -> WalkForwardResult:
    """Run walk-forward optimization.

    Splits data into n_splits train/test pairs, optimizes on train,
    evaluates on test.

    Parameters
    ----------
    data : Sequence[Any]
        Full dataset (e.g., list of Candle).
    param_grid : dict[str, Sequence[Any]]
        Parameter grid to search.
    run_fn : Callable
        Function that takes (params, data_subset) and returns BacktestResult.
    n_splits : int
        Number of train/test splits.
    score_fn : Callable | None
        Scoring function. Defaults to total_return.

    Returns
    -------
    WalkForwardResult
        Walk-forward optimization results.
    """
    if len(data) < n_splits * 2:
        raise ValueError(f"Need at least {n_splits * 2} data points for {n_splits} splits")

    split_size = len(data) // (n_splits + 1)
    all_in_sample: list[OptimizationResult] = []
    all_oos: list[OptimizationResult] = []

    for i in range(n_splits):
        train_start = i * split_size
        train_end = train_start + split_size
        test_end = min(train_end + split_size, len(data))

        train_data = data[train_start:train_end]
        test_data = data[train_end:test_end]

        # Optimize on train
        def train_run(params: dict[str, Any]) -> BacktestResult:
            return run_fn(params, train_data)

        grid = grid_search(param_grid, train_run, score_fn)

        if grid.best is not None:
            all_in_sample.append(grid.best)

            # Evaluate best params on test
            try:
                oos_result = run_fn(grid.best.params, test_data)
                score = score_fn(oos_result) if score_fn else oos_result.total_return
                all_oos.append(
                    OptimizationResult(params=grid.best.params, result=oos_result, score=score)
                )
            except Exception:
                continue

    # Aggregate
    best_params = all_in_sample[-1].params if all_in_sample else {}
    total_ret = sum(r.score for r in all_oos) / len(all_oos) if all_oos else 0.0

    return WalkForwardResult(
        in_sample_results=tuple(all_in_sample),
        out_of_sample_results=tuple(all_oos),
        best_params=best_params,
        total_return=total_ret,
    )


__all__ = [
    "GridSearchResult",
    "OptimizationResult",
    "WalkForwardResult",
    "grid_search",
    "walk_forward",
]
