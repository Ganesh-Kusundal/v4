"""grid_search default scoring (P0).

The default score must be risk-adjusted: raw total_return ranks a
high-return / high-drawdown grid point above a steadier one, which is
the overfitting trap grid search exists to escape.
"""

from __future__ import annotations

from tradex_replay.backtest import BacktestResult
from tradex_replay.optimization import (
    OptimizationResult,
    _default_score,
    grid_search,
)


def _result(
    total_return: float,
    sharpe: float,
    max_drawdown: float = 0.05,
) -> BacktestResult:
    return BacktestResult(
        total_return=total_return,
        sharpe=sharpe,
        max_drawdown=max_drawdown,
        num_trades=1,
    )


def test_default_score_is_sharpe() -> None:
    assert _default_score(_result(0.10, 1.5)) == 1.5


def test_default_score_nan_sharpe_is_zero() -> None:
    assert _default_score(_result(0.99, float("nan"))) == 0.0


def test_default_score_inf_sharpe_is_zero() -> None:
    assert _default_score(_result(0.99, float("inf"))) == 0.0


def test_optimization_result_default_score_is_sharpe() -> None:
    opt = OptimizationResult(params={"p": 1}, result=_result(0.10, 2.0))
    assert opt.score == 2.0


def test_grid_search_ranks_sharpe_over_return() -> None:
    """The steady 20%/2.0-Sharpe run must beat the 90%/0.3-Sharpe run."""
    hot = _result(0.90, 0.3, max_drawdown=0.50)
    steady = _result(0.20, 2.0)
    library = {"hot": hot, "steady": steady}
    gs = grid_search(
        {"name": ["hot", "steady"]},
        lambda params: library[params["name"]],
    )
    assert gs.best is not None
    assert gs.best.params == {"name": "steady"}


def test_explicit_score_fn_still_wins() -> None:
    hot = _result(0.90, 0.3)
    steady = _result(0.20, 2.0)
    library = {"hot": hot, "steady": steady}
    gs = grid_search(
        {"name": ["hot", "steady"]},
        lambda params: library[params["name"]],
        score_fn=lambda r: r.total_return,
    )
    assert gs.best is not None
    assert gs.best.params == {"name": "hot"}
