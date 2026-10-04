"""Walk-forward analysis engine.

Provides out-of-sample walk-forward testing with configurable
train/test windows and strategy re-optimization at each step.

Two windowing modes:

- :func:`run_walk_forward` — index-based windows over a single series.
- :func:`run_walk_forward_by_date` — calendar windows over (possibly
  multi-symbol) data, so each train/test window contains every instrument's
  bars for the covered dates.

Each fold is a **purged, embargoed, warm** window (AC-3):

- **warm** — the last ``warmup_bars`` bars of the training window are carried
  into the out of sample run, so an indicator with an ``n`` bar lookback emits
  from the first out of sample bar instead of going silent for the first ``n``
  of them.  Those bars are training data: they warm the run and are then
  excluded from the scored segment.
- **embargoed** — ``max(1, ceil(0.01 * window_length_bars))`` bars are dropped
  on the test side of each fold boundary, so the last training position cannot
  leak into the first out of sample bar.
- **purged** — training and test windows do not touch, and a window too short
  to be valid fails the report rather than being quietly shrunk.
- **floored** — a fold must contribute at least :data:`MIN_OOS_BARS_PER_FOLD`
  scored out of sample bars, checked by :func:`~tradex_analytics.
  research_validity.require_min_oos_bars` — the same guard ``grid_search``
  applies, declared once in ``analytics`` and re-exported here.  Each fold
  hands it its own scored count, because the guard is opt-in on that number:
  it cannot see the data, so a caller that declares nothing is not stopped.

**Nothing that goes wrong is turned into a number.** A fold that raises fails
the whole report (see :class:`WalkForwardFoldError`): dropping it would shrink
the out of sample span and the trade count, which makes every promotion
criterion easier to pass.  A bug that makes a strategy look better is worse than
a crash.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from tradex_analytics.reports import max_drawdown, sharpe_ratio
from tradex_analytics.research_validity import (
    MIN_OOS_BARS_PER_FOLD,
    InsufficientSampleError,
    require_min_oos_bars,
)
from tradex_domain import Timeframe

from tradex_replay.backtest import BacktestResult
from tradex_replay.optimization import _default_score

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_WARMUP_BARS",
    "EMBARGO_FRACTION",
    "MIN_OOS_BARS_PER_FOLD",
    "InsufficientSampleError",
    "OosSegment",
    "WalkForwardFoldError",
    "WalkForwardReport",
    "WalkForwardStep",
    "group_by_date",
    "iter_oos_segments",
    "iter_oos_segments_by_date",
    "oos_equity_segment",
    "require_min_oos_bars",
    "run_walk_forward",
    "run_walk_forward_by_date",
]

#: Bars carried from the training tail into each out of sample run: the longest
#: lookback in the default parameter set of every shipped strategy is 20
#: (``sma_cross`` slow, ``bollinger_breakout`` length, ``bracket_breakout``
#: lookback) and an indicator needs ``period + 1`` closes before it emits its
#: first value.  A strategy declaring a longer period — ``ema_ribbon_pullback``
#: carries 100 — passes ``max(indicator period) + 1`` for itself.
DEFAULT_WARMUP_BARS: int = 21

#: Fraction of the test window dropped on its leading edge.  One percent is the
#: conventional purged-cross-validation embargo: small enough to cost almost no
#: out of sample data, large enough to cover a bar of label bleed.
EMBARGO_FRACTION: float = 0.01

#: Every ``Timeframe`` value, as the string ``sharpe_ratio`` annualises on.
_KNOWN_FREQUENCIES: frozenset[str] = frozenset(str(t) for t in Timeframe)


class WalkForwardFoldError(RuntimeError):
    """A fold could not be run, or could not be run *validly*.

    Raised instead of degrading.  A fold dropped from the report shrinks the out
    of sample span and the trade count, so every promotion criterion — Sharpe,
    round trips, span days, deflated Sharpe — passes more easily.  The report
    does not exist; this is the failure the operator has to see.
    """

    def __init__(self, step_index: int, reason: str) -> None:
        super().__init__(f"walk-forward fold {step_index} failed: {reason}")
        self.step_index = step_index
        self.reason = reason


@dataclass(frozen=True, slots=True)
class WalkForwardStep:
    """One step in walk-forward analysis."""

    step_index: int
    train_start: int
    train_end: int
    #: The test window handed to this fold, in the runner's own index units
    #: (bar indices for :func:`run_walk_forward`, day offsets into the sorted
    #: date list for :func:`run_walk_forward_by_date`).  The embargo sits inside
    #: it and is reported separately in :attr:`embargo_bars`.
    test_start: int
    test_end: int
    best_params: dict[str, Any] = field(default_factory=dict)
    in_sample_result: BacktestResult | None = None
    out_of_sample_result: BacktestResult | None = None
    in_sample_score: float = 0.0
    out_of_sample_score: float = 0.0
    #: Training-tail bars carried into the out of sample run to warm it.
    warmup_bars: int = 0
    #: Bars dropped on the test side of the fold boundary.
    embargo_bars: int = 0
    #: Scored out of sample bars: the test window's bars minus the embargo.
    oos_bar_count: int = 0
    #: The out of sample equity segment, warmup excluded.  The carried bars are
    #: training data and may not enter this.
    oos_equity_curve: list[float] = field(default_factory=list)
    #: Timestamps of the first and last scored out of sample bar, when the
    #: bars carry one.  :attr:`oos_bar_count` bars in between are not claimed:
    #: under purged walk forward the span covers training windows and embargo
    #: gaps too.
    oos_start: datetime | None = None
    oos_end: datetime | None = None


@dataclass(frozen=True, slots=True)
class WalkForwardReport:
    """Complete walk-forward analysis report."""

    steps: tuple[WalkForwardStep, ...]
    aggregate_oos_return: float = 0.0
    aggregate_oos_sharpe: float = 0.0
    aggregate_oos_maxdd: float = 0.0
    total_steps: int = 0
    successful_steps: int = 0
    #: The frequency every aggregate was annualised at, read from the stored
    #: bars' ``timeframe`` column and asserted constant before the run.
    bar_frequency: str = "daily"
    #: Calendar days between the first and the last scored out of sample bar.
    #: A span, not a claim of continuous out of sample data.
    oos_span_days: float = 0.0
    #: The concatenated out of sample series, exactly the one
    #: ``aggregate_oos_sharpe`` and ``aggregate_oos_maxdd`` were read from.  It is
    #: here so a report is judged on the series it graded: a caller that
    #: recomputed a Sharpe from the per fold results would be reading a different
    #: object, which is the mistake this whole report exists to prevent.
    oos_equity_curve: tuple[float, ...] = ()

    @property
    def success_rate(self) -> float:
        """Fraction of steps that completed successfully.

        Always ``1.0`` for a report that exists: a fold that failed raised
        rather than being counted as unsuccessful here.
        """
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
            f"Aggregate OOS Sharpe: {self.aggregate_oos_sharpe:.2f}"
            f" (annualised at {self.bar_frequency})",
            f"OOS Span (days):      {self.oos_span_days:.2f}",
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
                f"warmup={step.warmup_bars} embargo={step.embargo_bars} "
                f"oos_bars={step.oos_bar_count} "
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
    warmup_bars: int = DEFAULT_WARMUP_BARS,
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
        Scoring function. Defaults to risk-adjusted Sharpe.
    purge_bars : int
        Override for the embargo length, in bars.  0 (the default) computes
        ``max(1, ceil(0.01 * test_window))``.
    warmup_bars : int
        Training-tail bars carried into each out of sample run.  Defaults to
        :data:`DEFAULT_WARMUP_BARS`; pass ``max(indicator period) + 1`` for
        the strategy under test.

    Returns
    -------
    WalkForwardReport
        Complete walk-forward analysis report.

    Raises
    ------
    ValueError
        If ``len(data) < train_window + test_window``, or if the stored bars
        carry more than one ``timeframe``.
    WalkForwardFoldError
        If any fold raises, or cannot supply the warmup, or cannot supply
        :data:`MIN_OOS_BARS_PER_FOLD` scored out of sample bars.
    """
    if len(data) < train_window + test_window:
        raise ValueError(
            f"Need at least {train_window + test_window} data points, got {len(data)}"
        )
    frequency = _bar_frequency(data)

    _step = step_size or test_window
    steps: list[WalkForwardStep] = []
    step_idx = 0
    start = 0

    while start + train_window + test_window <= len(data):
        train_end = start + train_window
        test_end = train_end + test_window
        steps.append(
            _fold(
                step_index=step_idx,
                train_start=start,
                train_end=train_end,
                test_start=train_end,
                test_end=test_end,
                train_data=data[start:train_end],
                test_data=data[train_end:test_end],
                run_fn=run_fn,
                optimize_fn=optimize_fn,
                score_fn=score_fn,
                warmup_bars=warmup_bars,
                embargo_override=purge_bars,
            )
        )
        step_idx += 1
        start += _step

    return _report(steps, frequency)


def run_walk_forward_by_date(
    data: Sequence[Any],
    run_fn: Callable[[dict[str, Any], Sequence[Any]], BacktestResult],
    optimize_fn: Callable[[Sequence[Any]], dict[str, Any]],
    *,
    train_days: int = 30,
    test_days: int = 15,
    score_fn: Callable[[BacktestResult], float] | None = None,
    purge_bars: int = 0,
    warmup_bars: int = DEFAULT_WARMUP_BARS,
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
        Scoring function. Defaults to risk-adjusted Sharpe.
    purge_bars : int
        Override for the embargo length, in bars.  0 (the default) computes
        ``max(1, ceil(0.01 * window_length_bars))`` over the test window's bars.
    warmup_bars : int
        Training-tail bars carried into each out of sample run.  Defaults to
        :data:`DEFAULT_WARMUP_BARS`; pass ``max(indicator period) + 1`` for
        the strategy under test.

    Returns
    -------
    WalkForwardReport
        Complete walk-forward analysis report (step indices are day offsets
        into the sorted date list, not bar indices).

    Notes
    -----
    The step stride is exactly *test_days* (no overlap), unlike
    :func:`run_walk_forward` which accepts a custom ``step_size``.  Bars are
    assumed to arrive in timestamp order within a day, so that the embargo
    takes the genuinely leading bars of the window.

    Raises
    ------
    ValueError
        If *data* is empty, or if the stored bars carry more than one
        ``timeframe``.
    WalkForwardFoldError
        If any fold raises, or cannot supply the warmup, or cannot supply
        :data:`MIN_OOS_BARS_PER_FOLD` scored out of sample bars.
    """
    if not data:
        raise ValueError("run_walk_forward_by_date: data is empty")
    frequency = _bar_frequency(data)

    by_date, dates = group_by_date(data)

    steps: list[WalkForwardStep] = []
    step_idx = 0
    start = 0
    while start + train_days + test_days <= len(dates):
        train_dates = dates[start:start + train_days]
        test_dates = dates[start + train_days:start + train_days + test_days]
        steps.append(
            _fold(
                step_index=step_idx,
                train_start=start,
                train_end=start + train_days,
                test_start=start + train_days,
                test_end=start + train_days + test_days,
                train_data=[c for d in train_dates for c in by_date[d]],
                test_data=[c for d in test_dates for c in by_date[d]],
                run_fn=run_fn,
                optimize_fn=optimize_fn,
                score_fn=score_fn,
                warmup_bars=warmup_bars,
                embargo_override=purge_bars,
            )
        )
        step_idx += 1
        start += test_days

    return _report(steps, frequency)


# ------------------------------------------------------- the same windows, unrun


@dataclass(frozen=True, slots=True)
class OosSegment:
    """One fold's out of sample window, derived without running anything.

    The validation entry point grades a walk forward run it did not produce, so
    it needs the run's *windows* as well as its results: the round trips it
    counts are paired against the scored bars, and the cost stress is a rerun of
    the same segment. Re-deriving the windows from a second implementation would
    be a way for the two to disagree, so these come from the same partition
    :func:`_fold` runs on — there is one definition of a fold's shape, and both
    callers use it.

    The three bar lists are the fold's out of sample run, in the order it is
    handed to a strategy:

    - :attr:`oos_input` — warmup, then the embargo gap, then the scored bars.
      This is what a rerun of the fold is given.
    - :attr:`scored_bars` — the embargo gap removed, and no warmup. These are
      the only bars a criterion may read: a trade opened on a warmup bar is a
      training trade, however profitable it turned out.
    - the embargo gap itself is in neither list, and is not scored or claimed.
    """

    step_index: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int
    warmup_bars: int
    embargo_bars: int
    oos_bar_count: int
    oos_input: list[Any] = field(default_factory=list)
    scored_bars: list[Any] = field(default_factory=list)


def group_by_date(data: Sequence[Any]) -> tuple[dict[date, list[Any]], list[date]]:
    """The bars bucketed by calendar date, and the sorted dates.

    Split out of :func:`run_walk_forward_by_date` so a caller that needs the same
    windows afterwards does not group them a second time and get a second
    ordering.
    """
    by_date: dict[date, list[Any]] = {}
    for item in data:
        by_date.setdefault(item.timestamp.date(), []).append(item)
    return by_date, sorted(by_date)


def iter_oos_segments(
    data: Sequence[Any],
    *,
    train_window: int,
    test_window: int,
    step_size: int | None = None,
    purge_bars: int = 0,
    warmup_bars: int = DEFAULT_WARMUP_BARS,
) -> Iterator[tuple[Sequence[Any], OosSegment]]:
    """Yield ``(training data, segment)`` for each fold :func:`run_walk_forward`
    would run, in the same order and the same number.

    The stride, the loop bound and the partition are the run's own, so a report
    built from these windows is measuring the segments the run actually used —
    which :func:`tradex_replay.validation` asserts by comparing the two counts
    rather than trusting it.
    """
    _step = step_size or test_window
    step_idx = 0
    start = 0
    while start + train_window + test_window <= len(data):
        train_end = start + train_window
        test_end = train_end + test_window
        yield (
            data[start:train_end],
            _segment(
                step_index=step_idx,
                train_start=start,
                train_end=train_end,
                test_start=train_end,
                test_end=test_end,
                train_data=data[start:train_end],
                test_data=data[train_end:test_end],
                warmup_bars=warmup_bars,
                embargo_override=purge_bars,
            ),
        )
        step_idx += 1
        start += _step


def iter_oos_segments_by_date(
    by_date: dict[date, list[Any]],
    dates: Sequence[date],
    *,
    train_days: int,
    test_days: int,
    purge_bars: int = 0,
    warmup_bars: int = DEFAULT_WARMUP_BARS,
) -> Iterator[tuple[Sequence[Any], OosSegment]]:
    """The calendar-windowed counterpart of :func:`iter_oos_segments`.

    Takes the grouping :func:`group_by_date` produced, because the caller has
    usually built it already to run the walk forward. Step indices are day
    offsets into *dates*, matching :func:`run_walk_forward_by_date`.
    """
    step_idx = 0
    start = 0
    while start + train_days + test_days <= len(dates):
        train_dates = dates[start:start + train_days]
        test_dates = dates[start + train_days:start + train_days + test_days]
        train_data = [c for d in train_dates for c in by_date[d]]
        test_data = [c for d in test_dates for c in by_date[d]]
        yield (
            train_data,
            _segment(
                step_index=step_idx,
                train_start=start,
                train_end=start + train_days,
                test_start=start + train_days,
                test_end=start + train_days + test_days,
                train_data=train_data,
                test_data=test_data,
                warmup_bars=warmup_bars,
                embargo_override=purge_bars,
            ),
        )
        step_idx += 1
        start += test_days


# --------------------------------------------------------------------------- helpers


def _bar_frequency(data: Sequence[Any]) -> str:
    """The annualisation frequency carried by the stored ``timeframe`` column.

    Read off the bars themselves, which is where the parquet ``timeframe``
    column lands, and asserted **constant across the whole dataset before the
    run**: a series that silently mixes a daily and an intraday stretch cannot
    be annualised by one factor, and taking the first bar's frequency is how a
    daily factor ends up scaling 94,500 periods a year.

    Bars carrying no timeframe at all (a plain ``list[int]``) leave the
    historical daily default, with a warning — the factor is a guess there, and
    a guessed factor is worth saying out loud.
    """
    seen: set[str] = set()
    for bar in data:
        timeframe = getattr(bar, "timeframe", None)
        if timeframe is not None:
            seen.add(str(timeframe))
    if not seen:
        log.warning(
            "bars carry no timeframe: annualising the OOS Sharpe as daily, which is "
            "a guess rather than a fact read from the data"
        )
        return "daily"
    if len(seen) > 1:
        raise ValueError(
            f"bar timeframe is not constant across the dataset: {sorted(seen)}. Every "
            "OOS metric is annualised by a single factor, so the run stops here."
        )
    frequency = next(iter(seen))
    if frequency not in _KNOWN_FREQUENCIES:
        raise ValueError(
            f"unknown bar timeframe {frequency!r}: sharpe_ratio would silently fall "
            f"back to the daily factor. Known: {sorted(_KNOWN_FREQUENCIES)}"
        )
    return frequency


def _embargo_bars(window_length_bars: int) -> int:
    """``max(1, ceil(0.01 * window_length_bars))`` — one fold's embargo.

    The floor of one is load bearing: a 60 bar test window rounds ``0.6`` down to
    nothing, and a zero bar embargo is the defect this replaces.
    """
    return max(1, math.ceil(EMBARGO_FRACTION * window_length_bars))


def oos_equity_segment(result: BacktestResult, oos_bar_count: int) -> list[float]:
    """The out of sample part of a run's equity curve.

    The engine seeds the curve with the initial capital and appends one
    mark-to-market point per bar, so the scored out of sample segment is the
    last ``oos_bar_count + 1`` points.  The warmup bars in front of it are
    training data: a cross there is a training trade, and letting it into the
    out of sample curve reports in sample profit as out of sample.

    Public because the cost stress in ``tradex_replay.validation`` takes the
    *same* slice off a rerun, and the two P&L figures it compares are only
    comparable if both were cut the same way.
    """
    curve = list(result.equity_curve)
    if not curve:
        return []
    keep = oos_bar_count + 1
    return curve[len(curve) - keep:] if len(curve) > keep else curve


def _oos_partition(
    *,
    step_index: int,
    train_data: Sequence[Any],
    test_data: Sequence[Any],
    warmup_bars: int,
    embargo_override: int,
) -> tuple[list[Any], list[Any], list[Any], int]:
    """The three slices of one fold's out of sample run, and the embargo.

    ``(warmup, embargo gap, scored bars, embargo length)``, in the order a fold's
    run sees them. Both :func:`_fold` and :func:`_segment` — and so both the run
    and the validation entry point that grades it — go through here, so "which
    bars are out of sample" has one answer in this module rather than two that
    can drift apart.

    The two guards live here too, for the same reason: a fold that cannot carry
    its warmup, or that would score fewer than
    :data:`MIN_OOS_BARS_PER_FOLD` bars, is not a smaller fold, it is a failed
    one.
    """
    if len(train_data) < warmup_bars:
        raise WalkForwardFoldError(
            step_index,
            f"the training window has {len(train_data)} bars and cannot carry "
            f"{warmup_bars} of warmup, so the out of sample run would start cold. "
            f"Widen the training window to at least {warmup_bars} bars.",
        )

    embargo = embargo_override or _embargo_bars(len(test_data))
    warmup = list(train_data[len(train_data) - warmup_bars:]) if warmup_bars else []
    gap = list(test_data[:embargo])
    oos_bars = list(test_data[embargo:])
    # The one argument that arms the floor, and it is the count this fold
    # actually scored.  The guard is opt-in on that number — ``grid_search``
    # cannot see the data either, and a caller that declares nothing is not
    # stopped — so a fold that stopped declaring it would report a 29 bar
    # result as though it were a 30 bar one.  ``InsufficientSampleError`` is
    # re-raised as the fold error the report promises, naming the fold, so
    # there is still no report to grade.
    try:
        require_min_oos_bars(len(oos_bars), minimum=MIN_OOS_BARS_PER_FOLD)
    except InsufficientSampleError as exc:
        raise WalkForwardFoldError(step_index, str(exc)) from exc
    return warmup, gap, oos_bars, embargo


def _segment(
    *,
    step_index: int,
    train_start: int,
    train_end: int,
    test_start: int,
    test_end: int,
    train_data: Sequence[Any],
    test_data: Sequence[Any],
    warmup_bars: int,
    embargo_override: int,
) -> OosSegment:
    warmup, gap, scored, embargo = _oos_partition(
        step_index=step_index,
        train_data=train_data,
        test_data=test_data,
        warmup_bars=warmup_bars,
        embargo_override=embargo_override,
    )
    return OosSegment(
        step_index=step_index,
        train_start=train_start,
        train_end=train_end,
        test_start=test_start,
        test_end=test_end,
        warmup_bars=warmup_bars,
        embargo_bars=embargo,
        oos_bar_count=len(scored),
        oos_input=[*warmup, *gap, *scored],
        scored_bars=scored,
    )


def _fold(
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
    warmup_bars: int,
    embargo_override: int,
) -> WalkForwardStep:
    """Optimize on the train window, then run in-sample and out-of-sample.

    Any exception raised by the run propagates as a :class:`WalkForwardFoldError`
    naming this fold.  There is no handler, deliberately: every handler this
    function used to carry was a place a failed fold became a zero score and
    the report continued without it.
    """
    warmup, gap, oos_bars, embargo = _oos_partition(
        step_index=step_index,
        train_data=train_data,
        test_data=test_data,
        warmup_bars=warmup_bars,
        embargo_override=embargo_override,
    )

    try:
        best_params = optimize_fn(train_data)
        in_sample = run_fn(best_params, train_data)
        in_score = score_fn(in_sample) if score_fn else _default_score(in_sample)
        # Warm first, then the embargo gap, then the scored bars.  The gap is
        # legitimately part of the history visible at the first out of sample
        # bar; it is simply not evaluated, scored or claimed.
        oos = run_fn(best_params, [*warmup, *gap, *oos_bars])
        oos_score = score_fn(oos) if score_fn else oos.total_return
    except Exception as exc:
        raise WalkForwardFoldError(step_index, f"{type(exc).__name__}: {exc}") from exc

    stamps = [getattr(bar, "timestamp", None) for bar in oos_bars]
    stamps = [s for s in stamps if s is not None]
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
        warmup_bars=warmup_bars,
        embargo_bars=embargo,
        oos_bar_count=len(oos_bars),
        oos_equity_curve=oos_equity_segment(oos, len(oos_bars)),
        oos_start=stamps[0] if stamps else None,
        oos_end=stamps[-1] if stamps else None,
    )


def _report(steps: Sequence[WalkForwardStep], frequency: str) -> WalkForwardReport:
    agg_return, agg_sharpe, agg_maxdd = _aggregate(steps, frequency)
    return WalkForwardReport(
        steps=tuple(steps),
        aggregate_oos_return=agg_return,
        aggregate_oos_sharpe=agg_sharpe,
        aggregate_oos_maxdd=agg_maxdd,
        total_steps=len(steps),
        # Every step in a report that exists completed; a fold that failed
        # raised instead of being counted as unsuccessful here.
        successful_steps=len(steps),
        bar_frequency=frequency,
        oos_span_days=_oos_span_days(steps),
        oos_equity_curve=tuple(_stitched_curve(steps)),
    )


def _oos_span_days(steps: Sequence[WalkForwardStep]) -> float:
    """Calendar days from the first to the last scored out of sample bar.

    Bars carrying no ``timestamp`` cannot place a span, and the report says
    ``0.0`` rather than inventing one from an index.
    """
    starts = [s.oos_start for s in steps if s.oos_start is not None]
    ends = [s.oos_end for s in steps if s.oos_end is not None]
    if not starts or not ends:
        return 0.0
    return (max(ends) - min(starts)).total_seconds() / 86_400.0


def _stitched_curve(steps: Sequence[WalkForwardStep]) -> list[float]:
    """The concatenated out of sample series every aggregate is read from.

    Segments are chained multiplicatively, each normalized to its own start, so
    a fold is measured on its own scale and then joined.  A result carrying no
    equity curve at all still gets a series, chained from its total return per
    fold: one observation per fold is coarse, but it is a series.

    Exposed on the report as :attr:`WalkForwardReport.oos_equity_curve` because
    the Sharpe and the drawdown are properties *of this curve*. A caller that
    re-derived the series from the per fold results would be reading a different
    object, and the one thing a validation report must not do is take a number
    from somewhere other than the series it graded.
    """
    segments = [s.oos_equity_curve for s in steps if len(s.oos_equity_curve) > 1]
    if segments:
        stitched: list[float] = [segments[0][0]]
        for seg in segments:
            base = seg[0] or 1.0
            norm = [x / base for x in seg[1:]]
            last = stitched[-1]
            stitched.extend(v * last for v in norm)
        return stitched

    chained = [1.0]
    for s in steps:
        if s.out_of_sample_result is not None:
            chained.append(chained[-1] * (1.0 + s.out_of_sample_result.total_return))
    return chained


def _aggregate(
    steps: Sequence[WalkForwardStep], frequency: str
) -> tuple[float, float, float]:
    """Stitched OOS equity curve — chain normalized segments multiplicatively.

    The Sharpe is the Sharpe of the *concatenated* returns, annualised at
    *frequency*.  A mean of per fold Sharpes is not that: it weights a 30 bar
    fold the same as a 3000 bar one, and it usually comes out higher, because
    the variance of a mean is smaller than the variance of the whole.
    """
    curve = _stitched_curve(steps)
    agg_return = (curve[-1] - curve[0]) / curve[0] if curve and curve[0] else 0.0
    sharpe = sharpe_ratio(_returns(curve), frequency=frequency)
    # A drawdown needs more than one observation per fold to mean anything, so
    # the curve-less fallback reports 0.0 rather than a number off one point per
    # fold — the same reason it is not read as "no drawdown occurred".
    if any(len(s.oos_equity_curve) > 1 for s in steps):
        return agg_return, sharpe, max_drawdown(curve)
    return agg_return, sharpe, 0.0


def _returns(curve: Sequence[float]) -> list[float]:
    return [
        (curve[i] - curve[i - 1]) / curve[i - 1]
        for i in range(1, len(curve))
        if curve[i - 1] != 0
    ]
