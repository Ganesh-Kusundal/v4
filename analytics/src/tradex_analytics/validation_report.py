"""The validation report, and the promotion bar as an executable assertion.

This module is the reason the rest of the programme exists. Lanes 1d and 1e made
the numbers honest — purged and embargoed folds that start warm, annualised at
the stored bar frequency, and a grid search that reports its own trial count —
and this is where those numbers stop being numbers to be argued about and
become a verdict: *"promote this strategy"* is an assertion with a verdict and a
list of failures, not a comparison a reader performs in their head.

**The report is the bar.** Every criterion reads one named field, and
``passed`` is *derived* from the six of them in ``__post_init__``. It is not a
constructor argument, so there is no call that can write ``passed=True`` over
metrics that do not support it — including the read path, where a stored row's
verdict is deliberately excluded from what a report is rebuilt from
(``ValidationReportRow.as_report_kwargs``). Rehydration recomputes.

The six, with the field each reads:

===================================  ===========================================
Criterion                             Field
===================================  ===========================================
``oos_sharpe >= 1.0``                 ``oos_sharpe``
``oos_round_trips >= 100``            ``oos_round_trips``
``oos_span_days >= 90``               ``oos_span_days``
``net_pnl_at_2x_costs > 0``           ``net_pnl_at_2x_costs``
``deflated_sharpe >= 0.0``            ``deflated_sharpe``
``param_stability_share_of_grid       ``param_stability_share_of_grid``
>= 0.60``
===================================  ===========================================

Three of those deserve their reasons stated here rather than in a review.

**The Sharpe is the Sharpe of the concatenated series.** Not a mean of per fold
Sharpes: that weights a 30 bar fold like a 3000 bar one, and the variance of a
mean is smaller than the variance of the whole, so it comes out *higher*.
:func:`concatenated_oos_sharpe` is the only way this module computes a Sharpe,
annualised at the frequency it is handed and at a risk free rate of 0. The
report itself is written by the research walk-forward entry point
(``tradex_replay.validation``), never by an HTTP route — a route is a
presentation surface, and a route that could produce the report could be asked
for a passing one.

**A round trip is not a fill.** ``oos_round_trips`` is counted by pairing
entries with the fills that close them, per instrument, and
:func:`round_trips_from_fills` follows the pairing the results surface already
uses (``pairRoundTrips`` in ``frontend/src/backtest.ts``). A scale-in closed by a
single order is **one** round trip, where FIFO *lot* matching would report two
matched portions — and the bar's floor is a ``>=``, so counting portions is the
flattering direction. The fill count stays available as ``oos_fills`` and is
never what the bar reads. Today ``BacktestResult.num_trades`` counts filled
orders, the chart route publishes it under the key ``num_trades`` and the
interface labels it "Trades"; that mislabelling is a live bug, and the two
columns in the report's table are separate for exactly this reason.

**Instrument identity is required, never guessed.** Positions are per
instrument, so a fill that cannot name its own — neither on the record nor by
the bar it filled against — is refused. Merging two symbols into one scalar
ledger invents round trips: a buy in one symbol and a sell in another reads as
a position taken to zero when no position was ever opened.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import Any

from tradex_analytics._round_trips import ValidationReportError, _field, round_trips_from_fills
from tradex_analytics.reports import sharpe_ratio
from tradex_analytics.trade_metrics import Trade, compute_statistics

# --------------------------------------------------------------------------- the bar

#: Floor on the out of sample Sharpe, computed on the concatenated series.
MIN_OOS_SHARPE: float = 1.0

#: Floor on out of sample round trips. A round trip is an entry and the fills
#: that close it, so this is a floor on *trades* and not on fills.
MIN_OOS_ROUND_TRIPS: int = 100

#: Floor on the calendar days between the first and the last out of sample bar.
#: Under purged walk forward the span contains training windows and embargo
#: gaps, so it is a span and not a claim of continuous out of sample data.
MIN_OOS_SPAN_DAYS: float = 90.0

#: Floor on the Sharpe corrected for the number of trials the grid search tried.
MIN_DEFATED_SHARPE: float = 0.0

#: Floor on the fraction of grid cells within one Sharpe unit of the optimum:
#: a plateau rather than a spike.
MIN_PARAM_STABILITY_SHARE: float = 0.60

#: The multiple the modelled slippage and fees are rerun at. Named so the
#: criterion cannot be read as "costs went up a bit".
COST_STRESS_MULTIPLIER: float = 2.0

__all__ = [
    "COST_STRESS_MULTIPLIER",
    "GATING_FIELDS",
    "MIN_DEFATED_SHARPE",
    "MIN_OOS_ROUND_TRIPS",
    "MIN_OOS_SHARPE",
    "MIN_OOS_SPAN_DAYS",
    "MIN_PARAM_STABILITY_SHARE",
    "PROMOTION_CRITERIA",
    "REPORT_FIELDS",
    "PromotionCriterion",
    "TradeValidationReport",
    "ValidationReportError",
    "concatenated_oos_sharpe",
    "count_round_trips",
    "oos_fills_in",
    "parameter_set_hash",
    "round_trips_from_fills",
]


@dataclass(frozen=True, slots=True)
class PromotionCriterion:
    """One promotion criterion, and the report field it reads.

    ``strict`` distinguishes the one ``>`` in the bar (``net_pnl_at_2x_costs
    > 0``) from the five ``>=``. The comparison is stored rather than encoded
    in a lambda so the criterion can describe itself, which is what the failure
    list and the safety surface read.
    """

    field: str
    threshold: float
    strict: bool = False

    def value_of(self, report: TradeValidationReport) -> float:
        return float(getattr(report, self.field))

    def satisfied_by(self, report: TradeValidationReport) -> bool:
        value = self.value_of(report)
        return value > self.threshold if self.strict else value >= self.threshold

    def describe(self) -> str:
        return f"{self.field} {'>' if self.strict else '>='} {self.threshold}"

    def failure(self, report: TradeValidationReport) -> str:
        return (
            f"{self.field}={self.value_of(report):g} does not meet "
            f"{self.field} {'>' if self.strict else '>='} {self.threshold:g}"
        )


#: The bar, in the spec's order. Six entries, and the count is pinned by a test
#: because a seventh criterion would change what "passes" means.
PROMOTION_CRITERIA: tuple[PromotionCriterion, ...] = (
    PromotionCriterion("oos_sharpe", MIN_OOS_SHARPE),
    PromotionCriterion("oos_round_trips", float(MIN_OOS_ROUND_TRIPS)),
    PromotionCriterion("oos_span_days", MIN_OOS_SPAN_DAYS),
    PromotionCriterion("net_pnl_at_2x_costs", 0.0, strict=True),
    PromotionCriterion("deflated_sharpe", MIN_DEFATED_SHARPE),
    PromotionCriterion("param_stability_share_of_grid", MIN_PARAM_STABILITY_SHARE),
)


@dataclass(frozen=True, slots=True)
class TradeValidationReport:
    """One strategy, one parameter set, judged against the promotion bar.

    Identity is ``report_id``, ``strategy_id`` and ``parameter_set_hash``.
    ``report_id`` is a run identity, not a content hash, so re-running the same
    inputs produces a new row's id while the numbers are reproducible from the
    inputs; the *key* the report is stored under is the pair the spec names.

    The verdict fields (``criteria``, ``survives_2x_costs``, ``passed``,
    ``failures``, ``failed_fields``) are ``init=False``: they are computed from
    the metrics in ``__post_init__`` and cannot be passed in. Nothing that
    assembles a report can assert a verdict over metrics that do not support it.
    """

    report_id: str
    strategy_id: str
    parameter_set_hash: str
    #: Sharpe of the **concatenated** out of sample equity curve, annualised at
    #: ``bar_frequency`` with a risk free rate of 0.
    oos_sharpe: float
    #: Entries and the fills that closed them, paired per instrument. Not fills.
    oos_round_trips: int
    #: Executed out of sample fill records. Available, never read by the bar.
    oos_fills: int
    #: Calendar days from the first to the last scored out of sample bar.
    oos_span_days: float
    #: Average net P&L per out of sample round trip.
    expectancy: float
    #: Gross profit over gross loss across the out of sample round trips.
    profit_factor: float
    #: Drawdown of the concatenated out of sample curve, in percent (negative).
    max_drawdown_pct: float
    #: ``GridSearchResult.deflated_sharpe``: observed Sharpe minus the expected
    #: maximum over the trials the search tried.
    deflated_sharpe: float
    #: ``GridSearchResult.param_stability_share_of_grid``.
    param_stability_share_of_grid: float
    #: Total net P&L of the out of sample segments rerun at
    #: ``COST_STRESS_MULTIPLIER`` times the modelled costs.
    net_pnl_at_2x_costs: float
    #: The frequency every annualised number above was computed at. Carried
    #: because a Sharpe without its annualisation factor is not checkable, and
    #: AC-3 requires the stored timeframe to be the one used.
    bar_frequency: str
    # ----------------------------------------------------------------- derived
    criteria: tuple[PromotionCriterion, ...] = field(
        init=False, default=PROMOTION_CRITERIA
    )
    survives_2x_costs: bool = field(init=False, default=False)
    passed: bool = field(init=False, default=False)
    #: One human-readable reason per failed criterion: what the field reads and
    #: what the bar asks of it. What the safety surface shows.
    failures: tuple[str, ...] = field(init=False, default=())
    #: The names of the failed criteria, for a caller that wants to branch.
    failed_fields: tuple[str, ...] = field(init=False, default=())

    def __post_init__(self) -> None:
        _require_metrics_present(self)
        _require_counts_sane(self)
        failed = [c for c in PROMOTION_CRITERIA if not c.satisfied_by(self)]
        object.__setattr__(self, "failures", tuple(c.failure(self) for c in failed))
        object.__setattr__(self, "failed_fields", tuple(c.field for c in failed))
        object.__setattr__(self, "passed", not failed)
        object.__setattr__(self, "survives_2x_costs", self.net_pnl_at_2x_costs > 0)

    @property
    def promotable(self) -> bool:
        """Whether this report clears the bar. A strategy with no report has no
        such object, and reads ``not_evaluated`` instead."""
        return self.passed

    @property
    def promotion_status(self) -> str:
        """``passed`` or ``not_promoted``; ``not_evaluated`` is the store's
        answer for a strategy that has no report at all."""
        return "passed" if self.passed else "not_promoted"


#: Metric fields a report is built from, in declaration order. The verdict is
#: not among them.
REPORT_FIELDS: tuple[str, ...] = tuple(
    f.name for f in fields(TradeValidationReport) if f.init
)

#: ``name -> Field`` for the same set, so a caller (and a test) can assert that
#: the verdict fields are not constructor arguments.
REPORT_FIELD_INFO: Mapping[str, Any] = {
    f.name: f for f in fields(TradeValidationReport)
}

#: The six fields the bar reads. Every one of them is a metric, so no criterion
#: is computed from the verdict or from a field nothing else populates.
GATING_FIELDS: tuple[str, ...] = tuple(c.field for c in PROMOTION_CRITERIA)

#: Which module produces each gating metric, named in the error a missing one
#: raises. A ``None`` here is the single most likely way a report could be
#: built with a criterion unevaluated, and it is a ``>= 0.0`` on two of them.
_METRIC_PRODUCERS: Mapping[str, str] = {
    "oos_sharpe": (
        "the concatenated out of sample equity curve (WalkForwardReport.oos_equity_curve)"
    ),
    "oos_round_trips": "round_trips_from_fills over the out of sample fill records",
    "oos_fills": "oos_fills_in over the out of sample fill records",
    "oos_span_days": "WalkForwardReport.oos_span_days",
    "deflated_sharpe": "GridSearchResult.deflated_sharpe (lane 1e)",
    "param_stability_share_of_grid": "GridSearchResult.param_stability_share_of_grid (lane 1e)",
    "net_pnl_at_2x_costs": (
        f"a rerun of the out of sample segments at {COST_STRESS_MULTIPLIER:g}x "
        "the modelled costs"
    ),
}


def _require_metrics_present(report: TradeValidationReport) -> None:
    for name in (*GATING_FIELDS, "expectancy", "profit_factor", "max_drawdown_pct"):
        value = getattr(report, name)
        if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationReportError(
                f"{name} is {value!r}. Every promotion criterion is computed from a "
                "named field, so a field that was never produced has to stop the "
                f"report rather than read as a number. Produce it from: "
                f"{_METRIC_PRODUCERS.get(name, 'the walk forward report')}."
            )


def _require_counts_sane(report: TradeValidationReport) -> None:
    for name in ("oos_round_trips", "oos_fills", "oos_span_days"):
        value = getattr(report, name)
        if value < 0:
            raise ValidationReportError(
                f"{name} is {value!r}: a count or a span cannot be negative, and a "
                "negative one would drop out of a ``>=`` comparison rather than "
                "fail it."
            )


# ------------------------------------------------------------------ the numbers


def parameter_set_hash(parameters: Mapping[str, Any]) -> str:
    """A stable identity for a parameter set.

    Canonical JSON with sorted keys, so two dicts that differ only in insertion
    order are the same parameter set, and two that differ in any value are not.
    16 hex characters of the SHA-256: a key, not a security boundary.
    """
    canonical = json.dumps(parameters, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def concatenated_oos_sharpe(
    equity_curve: Sequence[float],
    *,
    frequency: str,
    risk_free_rate: float = 0.0,
) -> float:
    """The Sharpe of one series, annualised at *frequency*.

    The out of sample Sharpe, on the curve the folds were stitched into. This
    function exists so there is exactly one way this module computes a Sharpe:
    a mean of per fold Sharpes is not the Sharpe of the concatenated series and
    is usually higher, so it is a build failure here rather than a reading
    nobody takes.
    """
    returns = [
        (float(equity_curve[i]) - float(equity_curve[i - 1]))
        / float(equity_curve[i - 1])
        for i in range(1, len(equity_curve))
        if equity_curve[i - 1] not in (0, 0.0)
    ]
    return sharpe_ratio(returns, risk_free_rate, frequency)


def count_round_trips(records: Sequence[Any], *, bars: Sequence[Any] = ()) -> int:
    """How many round trips *records* contain. The promotion bar's second
    criterion reads this and never the length of *records*."""
    return len(round_trips_from_fills(records, bars=bars))


def oos_fills_in(
    records: Sequence[Any],
    *,
    start: datetime | None,
    end: datetime | None,
) -> list[Any]:
    """The executed fill records inside ``[start, end]``.

    A fold's run is fed the carried training tail and the embargo gap before the
    scored window, so its fills include training trades. The window is the same
    one the scored equity segment is trimmed to, so the count and the curve
    describe the same bars.

    Records arriving with no placeable window are refused: counting the whole
    run would put training trades into the out of sample count, and quietly
    dropping them would understate a criterion that ought to fail.
    """
    if not records:
        return []
    if start is None or end is None:
        raise ValidationReportError(
            f"{len(records)} fill record(s) were produced but the out of sample "
            "window cannot be placed: the bars carry no timestamp, so no fill can "
            "be classified as in or out of sample. Counting them all would report "
            "training trades as out of sample."
        )
    return [
        record
        for record in records
        if not _field(record, "rejected")
        and start <= _field(record, "time") <= end
    ]


def report_statistics(
    trips: Sequence[Trade],
    *,
    equity_curve: Sequence[float],
    frequency: str,
) -> dict[str, float]:
    """``expectancy``, ``profit_factor`` and ``max_drawdown_pct`` for a report.

    Read off :func:`tradex_analytics.trade_metrics.compute_statistics`, the same
    definitions the results surface uses, so a report and a chart cannot disagree
    about the average trade. The drawdown comes from the equity curve rather
    than from the trips, because a drawdown measured between round trips is not
    a drawdown.
    """
    stats = compute_statistics(
        list(trips), equity_curve=list(equity_curve), frequency=frequency
    )
    return {
        "expectancy": float(stats.expectancy),
        "profit_factor": float(stats.profit_factor),
        "max_drawdown_pct": float(stats.max_drawdown_pct),
    }


def mint_report_id() -> str:
    """A run identity for a report that did not bring one."""
    return uuid.uuid4().hex
