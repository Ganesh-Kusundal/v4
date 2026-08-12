"""Parameter optimization for backtesting.

Provides grid search (optionally parallel) and walk-forward optimization
over strategy parameters.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
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
    *,
    max_workers: int = 1,
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
    max_workers : int
        Parallelism for evaluating combinations (default 1 = sequential).
        ``run_fn`` must be thread-safe when set > 1.

    Returns
    -------
    GridSearchResult
        Results from all parameter combinations (order preserved).

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

    def _evaluate(combo: tuple[Any, ...]) -> OptimizationResult | None:
        params = dict(zip(keys, combo))
        try:
            bt_result = run_fn(params)
            score = score_fn(bt_result) if score_fn else bt_result.total_return
            return OptimizationResult(params=params, result=bt_result, score=score)
        except Exception:
            return None

    if max_workers > 1:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            evaluated = list(pool.map(_evaluate, combinations))
    else:
        evaluated = [_evaluate(c) for c in combinations]

    results = [r for r in evaluated if r is not None]
    best = max(results, key=lambda r: r.score) if results else None

    return GridSearchResult(
        results=tuple(results),
        best=best,
        total_combinations=len(combinations),
    )


def make_grid_optimizer(
    param_grid: dict[str, Sequence[Any]],
    run_fn: Callable[[dict[str, Any], Sequence[Any]], BacktestResult],
    score_fn: Callable[[BacktestResult], float] | None = None,
    *,
    max_workers: int = 1,
) -> Callable[[Sequence[Any]], dict[str, Any]]:
    """Build a walk-forward ``optimize_fn`` from a parameter grid.

    The returned callable matches ``run_walk_forward``'s ``optimize_fn``
    signature: given training data, run ``grid_search`` over it (binding the
    data into ``run_fn``) and return the best params (``{}`` when nothing
    succeeded).

    Parameters
    ----------
    param_grid : dict[str, Sequence[Any]]
        Parameter grid passed through to :func:`grid_search`.
    run_fn : Callable
        Function taking ``(params, data_subset)`` and returning a BacktestResult.
    score_fn : Callable | None
        Scoring function passed through to :func:`grid_search`.
    max_workers : int
        Parallelism for each grid search (default 1 = sequential).

    Returns
    -------
    Callable[[Sequence[Any]], dict[str, Any]]
        An ``optimize_fn`` for ``run_walk_forward``.
    """

    def optimize_fn(train_data: Sequence[Any]) -> dict[str, Any]:
        result = grid_search(
            param_grid,
            lambda params: run_fn(params, train_data),
            score_fn=score_fn,
            max_workers=max_workers,
        )
        return dict(result.best.params) if result.best is not None else {}

    return optimize_fn


__all__ = [
    "GridSearchResult",
    "OptimizationResult",
    "grid_search",
    "make_grid_optimizer",
]
