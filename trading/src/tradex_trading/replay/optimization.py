"""Parameter optimization for backtesting.

Provides grid search and walk-forward optimization over strategy parameters.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from dataclasses import dataclass
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


__all__ = [
    "GridSearchResult",
    "OptimizationResult",
    "grid_search",
]
