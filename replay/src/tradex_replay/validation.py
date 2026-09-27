"""The research entry point that produces a ``TradeValidationReport``.

This is where Flow A of the production readiness programme ends: strategy spec
and parameter grid -> purged, embargoed, warm walk-forward -> statistical gate ->
cost stress -> a persisted report whose ``passed`` is computed.

**It is the only writer of a report, and it is not reachable from an HTTP
route.** That is the point of putting it here rather than in a route: a route is
a presentation surface, and a route that could produce the report could be asked
for a passing one. Nothing under ``interfaces/src`` may import this module, and a
test asserts that by walking the AST of the route tree — including the negative
control that proves the walk is looking at something.

What the entry point refuses to do, because each of these is a way to obtain a
verdict without evaluating a criterion:

* **It does not compute the Sharpe from the per fold numbers.**  It reads
  ``WalkForwardReport.oos_equity_curve`` — the chained out of sample series — and
  calls :func:`~tradex_analytics.validation_report.concatenated_oos_sharpe` on
  it.  ``BacktestResult.sharpe`` on each fold is not read at all, so a run whose
  per fold Sharpes are excellent and whose equity curve is flat produces a
  failing report, not a passing one.
* **It does not count fills as trades.**  ``oos_round_trips`` is counted by
  pairing entries with the fills that close them, per instrument, inside the
  scored window of each fold; ``oos_fills`` is kept beside it and never read by
  the bar.
* **It does not fill in a gate it was not given.**  ``deflated_sharpe`` and
  ``param_stability_share_of_grid`` come from the grid search the caller ran
  (lane 1e's ``GridSearchResult``), and ``None`` for either stops the report
  rather than defaulting it to a value that clears the bar.
* **It does not skip the cost stress.**  ``stress_run_fn`` is required: the out
  of sample segments are re-run at :data:`COST_STRESS_MULTIPLIER` times the
  modelled slippage and fees, on the *same* segments, and a stress run whose
  curve does not cover the segment it was supposed to re-run is refused rather
  than graded on less data.

The 30 out of sample bars per fold floor is not re-checked here: it is enforced
by the fold itself (lane 1d, over the same
:func:`~tradex_analytics.research_validity.require_min_oos_bars` the grid search
uses), so a window too short to judge fails the run before a verdict exists.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from typing import Any

from tradex_analytics.trade_metrics import Trade
from tradex_analytics.validation_report import (
    COST_STRESS_MULTIPLIER,
    TradeValidationReport,
    ValidationReportError,
    concatenated_oos_sharpe,
    mint_report_id,
    oos_fills_in,
    parameter_set_hash,
    report_statistics,
    round_trips_from_fills,
)
from tradex_execution.validation_report_store import ValidationReportStore

from tradex_replay.backtest import BacktestResult
from tradex_replay.walk_forward import (
    DEFAULT_WARMUP_BARS,
    OosSegment,
    WalkForwardReport,
    group_by_date,
    iter_oos_segments,
    iter_oos_segments_by_date,
    oos_equity_segment,
    run_walk_forward,
    run_walk_forward_by_date,
)

__all__ = [
    "COST_STRESS_MULTIPLIER",
    "ValidationRunError",
    "generate_validation_report",
    "generate_validation_report_by_date",
]

#: A run function, in the shape ``run_walk_forward`` takes: the parameters a
#: fold chose, and the bars to run them over.
RunFn = Callable[[dict[str, Any], Sequence[Any]], BacktestResult]


class ValidationRunError(RuntimeError):
    """The validation run cannot produce a report.

    Every one of these is a case where a number the verdict depends on does not
    exist yet, or does not describe the run being judged.  A loud failure is the
    only safe answer: a report that fills the gap with a default is a promotion
    decision resting on nothing.
    """


def generate_validation_report(
    data: Sequence[Any],
    *,
    strategy_id: str,
    parameters: dict[str, Any],
    run_fn: RunFn,
    optimize_fn: Callable[[Sequence[Any]], dict[str, Any]],
    stress_run_fn: RunFn | None,
    deflated_sharpe: float | None,
    param_stability_share_of_grid: float | None,
    train_window: int = 252,
    test_window: int = 63,
    step_size: int | None = None,
    purge_bars: int = 0,
    warmup_bars: int = DEFAULT_WARMUP_BARS,
    report_id: str | None = None,
    store: ValidationReportStore | None = None,
) -> TradeValidationReport:
    """Walk forward over bar-indexed windows and produce the promotion report.

    See :func:`generate_validation_report_by_date` for the parameters; this is
    the same report over bar windows rather than calendar ones.  Bars carrying
    no timestamp cannot place an out of sample span, so ``oos_span_days`` is
    ``0.0`` and criterion three fails — the run says it cannot place the span
    rather than inventing one that would clear the bar.
    """
    stress_run_fn, deflated_sharpe, param_stability_share_of_grid = (
        _require_costs_and_statistics(
            stress_run_fn, deflated_sharpe, param_stability_share_of_grid
        )
    )
    walk = run_walk_forward(
        data,
        run_fn,
        optimize_fn,
        train_window=train_window,
        test_window=test_window,
        step_size=step_size,
        purge_bars=purge_bars,
        warmup_bars=warmup_bars,
    )
    return _build(
        walk,
        segments=iter_oos_segments(
            data,
            train_window=train_window,
            test_window=test_window,
            step_size=step_size,
            purge_bars=purge_bars,
            warmup_bars=warmup_bars,
        ),
        strategy_id=strategy_id,
        parameters=parameters,
        stress_run_fn=stress_run_fn,
        deflated_sharpe=deflated_sharpe,
        param_stability_share_of_grid=param_stability_share_of_grid,
        report_id=report_id,
        store=store,
    )


def generate_validation_report_by_date(
    data: Sequence[Any],
    *,
    strategy_id: str,
    parameters: dict[str, Any],
    run_fn: RunFn,
    optimize_fn: Callable[[Sequence[Any]], dict[str, Any]],
    stress_run_fn: RunFn | None,
    deflated_sharpe: float | None,
    param_stability_share_of_grid: float | None,
    train_days: int = 30,
    test_days: int = 15,
    purge_bars: int = 0,
    warmup_bars: int = DEFAULT_WARMUP_BARS,
    report_id: str | None = None,
    store: ValidationReportStore | None = None,
) -> TradeValidationReport:
    """Walk forward over calendar-date windows and produce the promotion report.

    Parameters
    ----------
    data:
        The bars, in timestamp order. Each must carry a ``timestamp`` (and a
        ``timeframe``, which is read as the annualisation factor and asserted
        constant across the whole set).
    strategy_id:
        Whose strategy this is. Together with the parameter set hash it is the
        report's identity, and the key it is stored under.
    parameters:
        The parameter set being judged. Hashed into
        ``parameter_set_hash``; the report does not keep the parameters
        themselves, so a stored report says which set produced it without
        embedding it.
    run_fn:
        Runs a strategy over a slice of bars: ``(params, bars) -> BacktestResult``.
    optimize_fn:
        Chooses the parameters for a training window: ``(train bars) -> params``.
    stress_run_fn:
        The same shape as *run_fn*, configured for
        ``COST_STRESS_MULTIPLIER`` times the modelled slippage and fees. Required.
    deflated_sharpe:
        ``GridSearchResult.deflated_sharpe`` from the search that chose these
        parameters. Required, and ``None`` stops the report.
    param_stability_share_of_grid:
        ``GridSearchResult.param_stability_share_of_grid``, likewise.
    report_id:
        Optional run identity. A fresh one is minted when omitted.
    store:
        Optional :class:`~tradex_execution.validation_report_store.
        ValidationReportStore`; when given, the report is persisted under
        ``(strategy_id, parameter_set_hash)``.

    Returns
    -------
    TradeValidationReport
        With ``passed`` computed from the six criteria and ``failures`` naming
        the ones that did not.
    """
    stress_run_fn, deflated_sharpe, param_stability_share_of_grid = (
        _require_costs_and_statistics(
            stress_run_fn, deflated_sharpe, param_stability_share_of_grid
        )
    )
    walk = run_walk_forward_by_date(
        data,
        run_fn,
        optimize_fn,
        train_days=train_days,
        test_days=test_days,
        purge_bars=purge_bars,
        warmup_bars=warmup_bars,
    )
    by_date, dates = group_by_date(data)
    return _build(
        walk,
        segments=iter_oos_segments_by_date(
            by_date,
            dates,
            train_days=train_days,
            test_days=test_days,
            purge_bars=purge_bars,
            warmup_bars=warmup_bars,
        ),
        strategy_id=strategy_id,
        parameters=parameters,
        stress_run_fn=stress_run_fn,
        deflated_sharpe=deflated_sharpe,
        param_stability_share_of_grid=param_stability_share_of_grid,
        report_id=report_id,
        store=store,
    )


def _require_costs_and_statistics(
    stress_run_fn: RunFn | None,
    deflated_sharpe: float | None,
    param_stability_share_of_grid: float | None,
) -> tuple[RunFn, float, float]:
    """Return the three, narrowed, or raise naming the one that is missing."""
    """The three inputs a report cannot invent.

    Each of these is a criterion, so a default would be a criterion evaluated
    against nothing: the cost stress defaulting to the base run's P&L is
    criterion four passing by assumption, and the two statistics defaulting to
    ``0.0`` clear ``>= 0.0`` and ``>= 0.60`` respectively.
    """
    if stress_run_fn is None:
        raise ValidationRunError(
            "no cost stress run was supplied. Criterion four is net P&L with the "
            f"modelled slippage and fees at {COST_STRESS_MULTIPLIER:g}x, which is a "
            "rerun of the out of sample segments and not a number this module can "
            "derive. Pass stress_run_fn, the same shape as run_fn, configured for "
            "the stressed costs."
        )
    if deflated_sharpe is None:
        raise ValidationRunError(
            "deflated_sharpe is missing, so criterion five is unevaluated. It "
            "comes from the search that chose these parameters: pass "
            "GridSearchResult.deflated_sharpe (lane 1e). It is None rather than "
            "0.0 on purpose, because 0.0 clears a '>= 0.0' bar."
        )
    if param_stability_share_of_grid is None:
        raise ValidationRunError(
            "param_stability_share_of_grid is missing, so criterion six is "
            "unevaluated. It comes from the search that chose these parameters: "
            "pass GridSearchResult.param_stability_share_of_grid (lane 1e)."
        )
    return stress_run_fn, deflated_sharpe, param_stability_share_of_grid


def _build(
    walk: WalkForwardReport,
    *,
    segments: Iterator[tuple[Sequence[Any], OosSegment]],
    strategy_id: str,
    parameters: dict[str, Any],
    stress_run_fn: RunFn,
    deflated_sharpe: float,
    param_stability_share_of_grid: float,
    report_id: str | None,
    store: ValidationReportStore | None,
) -> TradeValidationReport:
    """Assemble the report from a walk-forward run and the same windows."""
    windows = [segment for _train_data, segment in segments]
    if not walk.steps:
        raise ValidationRunError(
            f"walk forward produced no folds for {strategy_id!r}, so there is no "
            "out of sample evidence to judge. A strategy with no report reads as "
            "not_evaluated, which is not the same as evaluated and failed."
        )
    if len(windows) != walk.total_steps:
        raise ValidationRunError(
            f"walk forward ran {walk.total_steps} fold(s) but its windows number "
            f"{len(windows)}. The report would be measuring segments the run did "
            "not use."
        )

    trips: list[Trade] = []
    fills = 0
    net_pnl_at_2x_costs = 0.0
    for step, segment in zip(walk.steps, windows, strict=True):
        result = step.out_of_sample_result
        if result is None:  # pragma: no cover - a step always carries its run
            raise ValidationRunError(f"fold {segment.step_index} has no out of sample result.")
        # The scored window, not the run: the carried warmup and the embargo gap
        # are training bars, and a trade opened on one of them is not an out of
        # sample trade however profitable it turned out.
        records = oos_fills_in(result.fills, start=step.oos_start, end=step.oos_end)
        fills += len(records)
        trips.extend(round_trips_from_fills(records, bars=segment.scored_bars))
        net_pnl_at_2x_costs += _stress_pnl(step, segment, stress_run_fn)

    curve = walk.oos_equity_curve
    if not curve:
        raise ValidationRunError(
            "the walk forward report carries no out of sample equity curve, so "
            "there is no series to take a Sharpe or a drawdown from."
        )
    try:
        report = TradeValidationReport(
            report_id=report_id or mint_report_id(),
            strategy_id=strategy_id,
            parameter_set_hash=parameter_set_hash(parameters),
            oos_sharpe=concatenated_oos_sharpe(curve, frequency=walk.bar_frequency),
            oos_round_trips=len(trips),
            oos_fills=fills,
            oos_span_days=walk.oos_span_days,
            deflated_sharpe=float(deflated_sharpe),
            param_stability_share_of_grid=float(param_stability_share_of_grid),
            net_pnl_at_2x_costs=net_pnl_at_2x_costs,
            bar_frequency=walk.bar_frequency,
            **report_statistics(
                trips, equity_curve=curve, frequency=walk.bar_frequency
            ),
        )
    except ValidationReportError as exc:
        raise ValidationRunError(str(exc)) from exc

    if store is not None:
        store.save(report)
    return report


def _stress_pnl(
    step: Any, segment: OosSegment, stress_run_fn: RunFn
) -> float:
    """The net P&L of one fold's out of sample segment at stressed costs.

    The same segment, re-run: the parameters the fold chose, the same warmup and
    embargo and scored bars, and nothing else.  Re-optimising under the stressed
    costs would grade a different strategy, and re-running the training window
    would cost time to learn nothing the report reads.

    The P&L is summed per fold in currency, which is what "total net profit and
    loss" means when every fold is an independent run opening with the same
    capital.  It is deliberately *not* the compounded chained curve: that one
    describes a continuous account, and these are not.
    """
    stressed = stress_run_fn(dict(step.best_params), list(segment.oos_input))
    # The engine seeds the curve with the initial capital and marks it once per
    # bar, so a run over this window returns ``len(window) + 1`` points.  Fewer
    # means the stress run covered less than the base run did, and the two P&L
    # figures would not be comparable — the one comparison criterion four makes.
    expected = len(segment.oos_input) + 1
    if len(stressed.equity_curve) < expected:
        raise ValidationRunError(
            f"fold {segment.step_index}: the cost stress run returned "
            f"{len(stressed.equity_curve)} curve point(s) for the "
            f"{len(segment.oos_input)}-bar window it was handed, which needs "
            f"{expected}. The stressed P&L is only comparable with the base run "
            "if it covers the same bars, so this is refused rather than graded on "
            "less data."
        )
    segment_curve = oos_equity_segment(stressed, segment.oos_bar_count)
    return segment_curve[-1] - segment_curve[0]
