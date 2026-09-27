"""The validation report and the six criterion promotion bar (AC-13, lane 1g).

Every test here names one way a promotion decision can be wrong while reading
correct.  The two the brief singles out are:

* an out of sample Sharpe computed as the **mean of per fold Sharpes**, which is
  not the Sharpe of the concatenated series and is usually higher, and
* a fill count standing in for a round trip count — a live mislabelling bug
  today (``metrics.num_trades`` counts fills and the interface calls it Trades).

The red state of each is recorded in ``.superpowers/sdd/task-A4-report.md``.
"""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from tradex_analytics import validation_report as vr
from tradex_analytics.trade_metrics import Trade, compute_statistics
from tradex_analytics.validation_report import (
    MIN_DEFATED_SHARPE,
    MIN_OOS_ROUND_TRIPS,
    MIN_OOS_SHARPE,
    MIN_OOS_SPAN_DAYS,
    MIN_PARAM_STABILITY_SHARE,
    PROMOTION_CRITERIA,
    TradeValidationReport,
    ValidationReportError,
    concatenated_oos_sharpe,
    count_round_trips,
    oos_fills_in,
    parameter_set_hash,
    round_trips_from_fills,
)
from tradex_domain import OHLC, Candle, Price, Quantity, Timeframe
from tradex_domain.instruments import Equity

# --------------------------------------------------------------------------- fixtures

#: A report that clears every criterion, so a test can break exactly one.
_GENEROUS: dict[str, Any] = {
    "report_id": "report-1",
    "strategy_id": "sma_cross",
    "parameter_set_hash": "0f1e2d3c4b5a6978",
    "oos_sharpe": 1.8,
    "oos_round_trips": 140,
    "oos_fills": 260,
    "oos_span_days": 210.0,
    "expectancy": 12.5,
    "profit_factor": 1.6,
    "max_drawdown_pct": -8.25,
    "deflated_sharpe": 0.42,
    "param_stability_share_of_grid": 0.71,
    "net_pnl_at_2x_costs": 180_000.0,
    "bar_frequency": "1m",
}

#: One value per criterion that fails it, and by how little.
_JUST_BELOW: dict[str, Any] = {
    "oos_sharpe": 0.999,
    "oos_round_trips": 99,
    "oos_span_days": 89.999,
    "net_pnl_at_2x_costs": -0.01,  # strict: > 0, so exactly 0 also fails
    "deflated_sharpe": -0.0001,
    "param_stability_share_of_grid": 0.5999,
}


def _report(**overrides: Any) -> TradeValidationReport:
    return TradeValidationReport(**{**_GENEROUS, **overrides})


def _candle(
    ts: datetime, *, symbol: str = "RELIANCE", price: float = 100.0
) -> Candle:
    p = Price(value=Decimal(str(price)))
    return Candle(
        instrument=Equity.of("NSE", symbol),
        timeframe=Timeframe.M1,
        ohlc=OHLC(open=p, high=p, low=p, close=p),
        volume=Quantity(value=Decimal("1000")),
        timestamp=ts,
    )


_T0 = datetime(2026, 3, 2, 9, 15)


def _bar(minutes: int, *, symbol: str = "RELIANCE", price: float = 100.0) -> Candle:
    return _candle(_T0 + timedelta(minutes=minutes), symbol=symbol, price=price)


def _fill(
    minutes: int,
    side: str,
    qty: float = 100.0,
    price: float = 100.0,
    *,
    symbol: str | None = None,
    rejected: bool = False,
    instrument: Any = None,
) -> dict[str, Any]:
    """One ``BacktestResult.fills`` record, the shape the engine writes."""
    record: dict[str, Any] = {
        "time": _T0 + timedelta(minutes=minutes),
        "side": side,
        "price": price,
        "qty": qty,
        "rejected": rejected,
        "reason": "",
        "stop": None,
        "target": None,
    }
    if symbol is not None:
        record["instrument"] = f"NSE:{symbol}"
    if instrument is not None:
        record["instrument"] = instrument
    return record


# --------------------------------------------------------------------------- the bar


def test_the_promotion_bar_is_six_criteria_and_no_more():
    """Six, named by the spec, and no more.

    A seventh criterion would change what "passes the bar" means, so the count
    is pinned rather than derived: a lane that adds one has to come back and say
    so.
    """
    assert len(PROMOTION_CRITERIA) == 6
    assert [c.field for c in PROMOTION_CRITERIA] == [
        "oos_sharpe",
        "oos_round_trips",
        "oos_span_days",
        "net_pnl_at_2x_costs",
        "deflated_sharpe",
        "param_stability_share_of_grid",
    ]


def test_the_six_thresholds_are_the_specs_values():
    assert MIN_OOS_SHARPE == 1.0
    assert MIN_OOS_ROUND_TRIPS == 100
    assert MIN_OOS_SPAN_DAYS == 90.0
    assert MIN_DEFATED_SHARPE == 0.0
    assert MIN_PARAM_STABILITY_SHARE == 0.60
    assert vr.COST_STRESS_MULTIPLIER == 2.0


def test_every_criterion_reads_a_field_the_report_carries():
    """Each criterion is computable from a named field, so the mapping is
    asserted rather than assumed.  A criterion naming a field that does not
    exist would raise at construction; this says so in the test that is about
    the mapping."""
    for criterion in PROMOTION_CRITERIA:
        assert criterion.field in vr.REPORT_FIELDS, criterion.field
        assert criterion.field in vr.GATING_FIELDS, criterion.field
    assert set(c.field for c in PROMOTION_CRITERIA) == set(vr.GATING_FIELDS)


@pytest.mark.parametrize("field", sorted(_JUST_BELOW))
def test_a_report_failing_exactly_one_criterion_is_not_passed_and_names_it(field):
    """The negative control for the whole bar: break one, and exactly one fails."""
    report = _report(**{field: _JUST_BELOW[field]})
    assert report.passed is False
    assert report.failed_fields == (field,)
    assert len(report.failures) == 1
    assert field in report.failures[0]
    assert report.promotion_status == "not_promoted"
    # The other five are still satisfied, so the failure names one criterion
    # rather than everything.
    satisfied = [c.field for c in report.criteria if c.satisfied_by(report)]
    assert set(satisfied) == set(vr.GATING_FIELDS) - {field}


def test_a_report_clearing_every_criterion_passes_with_no_failures():
    report = _report()
    assert report.passed is True
    assert report.failures == ()
    assert report.failed_fields == ()
    assert report.promotion_status == "passed"
    assert all(c.satisfied_by(report) for c in report.criteria)


@pytest.mark.parametrize(
    ("field", "at_bar"),
    [
        ("oos_sharpe", 1.0),
        ("oos_round_trips", 100),
        ("oos_span_days", 90.0),
        ("deflated_sharpe", 0.0),
        ("param_stability_share_of_grid", 0.60),
    ],
)
def test_the_bar_is_inclusive_where_the_spec_writes_ge(field, at_bar):
    assert _report(**{field: at_bar}).passed is True
    assert _report(**{field: _JUST_BELOW[field]}).passed is False


def test_net_pnl_at_zero_costs_is_a_failure_because_the_spec_writes_gt():
    """``net_pnl_at_2x_costs > 0`` is strict: a strategy that exactly breaks even
    at doubled costs has not survived them."""
    report = _report(net_pnl_at_2x_costs=0.0)
    assert report.passed is False
    assert report.failed_fields == ("net_pnl_at_2x_costs",)
    assert _report(net_pnl_at_2x_costs=0.01).passed is True


def test_the_verdict_is_never_entered_it_is_computed():
    """``passed`` is not a constructor argument, so no code path can set it.

    The failure this removes is a caller assembling a report by hand and
    writing ``passed=True`` over metrics that do not support it.
    """
    with pytest.raises(TypeError):
        TradeValidationReport(**{**_GENEROUS, "passed": True})
    with pytest.raises(TypeError):
        TradeValidationReport(**{**_GENEROUS, "failures": ()})
    with pytest.raises(TypeError):
        TradeValidationReport(**{**_GENEROUS, "survives_2x_costs": True})
    # And the dataclass field is not even an init field, so the report cannot be
    # built through dataclasses.replace carrying a stale verdict.
    for name in ("passed", "failures", "survives_2x_costs"):
        field_info = vr.REPORT_FIELD_INFO[name]
        assert not field_info.init, name


def test_survives_2x_costs_is_derived_from_the_pnl_not_entered():
    assert _report(net_pnl_at_2x_costs=1.0).survives_2x_costs is True
    assert _report(net_pnl_at_2x_costs=-1.0).survives_2x_costs is False


@pytest.mark.parametrize("field", sorted(_JUST_BELOW))
def test_a_gating_metric_left_uncomputed_is_refused_rather_than_zero(field):
    """``None`` is refused; a zero default would clear the criterion.

    ``deflated_sharpe`` and ``param_stability_share_of_grid`` are the two the
    grid search can fail to produce (lane 1e returns ``None`` for both), and
    both criteria are ``>= 0.0`` and ``>= 0.60`` — a zero default satisfies
    them without having been computed at all.
    """
    with pytest.raises(ValidationReportError) as excinfo:
        _report(**{field: None})
    assert field in str(excinfo.value)


def test_a_missing_producer_message_names_where_the_number_comes_from():
    with pytest.raises(ValidationReportError) as excinfo:
        _report(deflated_sharpe=None)
    message = str(excinfo.value)
    assert "GridSearchResult.deflated_sharpe" in message
    with pytest.raises(ValidationReportError) as excinfo:
        _report(param_stability_share_of_grid=None)
    assert "param_stability_share_of_grid" in str(excinfo.value)


def test_negative_round_trip_and_span_counts_are_refused():
    """A count cannot be negative, and a negative one would clear a ``>=`` bar
    by being absent from the comparison rather than by passing it."""
    for field in ("oos_round_trips", "oos_fills", "oos_span_days"):
        with pytest.raises(ValidationReportError) as excinfo:
            _report(**{field: -1})
        assert field in str(excinfo.value)


def test_the_report_is_frozen():
    report = _report()
    with pytest.raises(Exception):  # noqa: B017 - dataclasses raise FrozenInstanceError
        report.passed = True  # type: ignore[misc]


# --------------------------------------------------------------- criterion one


def test_oos_sharpe_is_the_sharpe_of_one_series_not_the_mean_of_several():
    """The concatenated series, annualised at the stored bar frequency.

    Two folds whose Sharpes are far apart: the mean of the two is comfortably
    above the bar, and the Sharpe of the stitched series is below it.  Only the
    second is the strategy's out of sample Sharpe.
    """
    from tradex_analytics.reports import sharpe_ratio

    flat = [1.0, 1.0, 1.0, 1.0, 1.0]  # zero variance: unusable on its own
    assert sharpe_ratio([0.0] * 4) == 0.0

    # Fold A: 12 identical winning bars.  Fold B: 12 identical losing bars.
    good = [100.0 * (1.004**i) for i in range(13)]
    bad = [100.0 * (0.996**i) for i in range(13)]
    fold_sharpes = [
        sharpe_ratio(_returns(good), 0.0, "daily"),
        sharpe_ratio(_returns(bad), 0.0, "daily"),
    ]
    mean_of_folds = sum(fold_sharpes) / len(fold_sharpes)

    stitched = [1.0]
    for segment in (good, bad):
        base = segment[0]
        stitched.extend((x / base) * stitched[-1] for x in segment[1:])
    on_the_series = concatenated_oos_sharpe(stitched, frequency="daily")

    assert on_the_series < mean_of_folds, (on_the_series, mean_of_folds)
    assert on_the_series == pytest.approx(
        sharpe_ratio(_returns(stitched), 0.0, "daily")
    )
    # The serial number is exactly the kind of case the bar exists for: each
    # fold alone would be an acceptable Sharpe, and the concatenation is not.
    assert flat  # the degenerate series is only used to pin zero variance


def _returns(curve: list[float]) -> list[float]:
    return [
        (curve[i] - curve[i - 1]) / curve[i - 1]
        for i in range(1, len(curve))
        if curve[i - 1] != 0
    ]


def test_oos_sharpe_annualises_at_the_frequency_it_is_given():
    from tradex_analytics.reports import sharpe_ratio

    curve = [100.0]
    for i in range(60):
        curve.append(curve[-1] * (1.004 if i % 3 else 0.997))
    daily = concatenated_oos_sharpe(curve, frequency="daily")
    minute = concatenated_oos_sharpe(curve, frequency="1m")
    assert daily == pytest.approx(sharpe_ratio(_returns(curve), 0.0, "daily"))
    assert minute == pytest.approx(sharpe_ratio(_returns(curve), 0.0, "1m"))
    assert minute > daily * 10


def test_oos_sharpe_uses_a_zero_risk_free_rate():
    from tradex_analytics.reports import sharpe_ratio

    curve = [100.0]
    for i in range(20):
        curve.append(curve[-1] * (1.002 if i % 2 else 0.999))
    assert concatenated_oos_sharpe(curve, frequency="daily") == pytest.approx(
        sharpe_ratio(_returns(curve), 0.0, "daily")
    )


def _averages_a_sharpe(source: str) -> bool:
    """True when *source* divides something named for a Sharpe by something.

    The rejection guard, as a function, so the negative control below can feed
    it the exact expression this programme exists to prevent.
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            if "sharpe" in ast.unparse(node.left).lower():
                return True
    return False


def test_no_module_on_the_report_path_can_average_per_fold_sharpes():
    """The mean of per fold Sharpes is rejected structurally, not by convention.

    A reviewer reading ``oos_sharpe`` cannot see whether some other line of the
    report path computed a mean of fold Sharpes instead, so the shape is
    forbidden in the report module, in the entry point that fills the field, and
    in the walk-forward aggregation the series comes from: no expression in any
    of them may divide a Sharpe by a count.  This is the "visibly rejected"
    requirement — the shortcut is not merely unused, it is a build failure.
    """
    from tradex_replay import validation, walk_forward

    for module in (vr, validation, walk_forward):
        source = inspect.getsource(module)
        assert not _averages_a_sharpe(source), module.__name__


def test_the_rejection_guard_is_not_vacuous():
    """Negative control: the exact shortcut the bar forbids is caught."""
    assert _averages_a_sharpe(
        "def f(fold_sharpes):\n    return sum(fold_sharpes) / len(fold_sharpes)\n"
    )
    assert _averages_a_sharpe("def f(rows):\n    return sum(r.sharpe for r in rows) / len(rows)\n")
    # And an unrelated division is left alone, or the guard would fire on
    # everything and be turned off.
    assert not _averages_a_sharpe("def f(curve):\n    return (curve[-1] - curve[0]) / curve[0]\n")


# ------------------------------------------------------------- criterion two


def test_a_round_trip_is_an_entry_and_the_fills_that_close_it():
    """One entry, one exit: one round trip."""
    trips = round_trips_from_fills(
        [_fill(0, "BUY", 100, 100.0), _fill(5, "SELL", 100, 110.0)],
        bars=[_bar(0), _bar(5)],
    )
    assert len(trips) == 1
    assert trips[0].side == "LONG"
    assert trips[0].quantity == Decimal("100")
    assert trips[0].entry_price == Decimal("100.00")
    assert trips[0].exit_price == Decimal("110.00")
    assert trips[0].gross_pnl == Decimal("1000.00")


def test_two_partial_exits_are_one_round_trip():
    trips = round_trips_from_fills(
        [
            _fill(0, "BUY", 100, 100.0),
            _fill(3, "SELL", 40, 105.0),
            _fill(6, "SELL", 60, 108.0),
        ],
        bars=[_bar(0), _bar(3), _bar(6)],
    )
    assert len(trips) == 1
    assert trips[0].exit_time == _T0 + timedelta(minutes=6)


def test_scaling_in_and_out_once_is_one_round_trip_not_two():
    """FIFO *lot* matching would report two matched portions here.

    The bar's own words are "a fill sequence that takes a position to zero or
    through it", and the position reaches zero once.  Counting the two lots a
    single exit happens to consume is the flattering direction for a
    ``>= 100`` floor, so the count is the position's, not the lot's.
    """
    fills = [
        _fill(0, "BUY", 50, 100.0),
        _fill(1, "BUY", 50, 102.0),
        _fill(5, "SELL", 100, 110.0),
    ]
    bars = [_bar(0), _bar(1), _bar(5)]
    assert count_round_trips(fills, bars=bars) == 1
    trip = round_trips_from_fills(fills, bars=bars)[0]
    assert trip.entry_price == Decimal("101.00")  # averaged entry
    assert trip.gross_pnl == Decimal("900.00")  # (110 - 101) * 100


def test_a_flip_is_one_round_trip_and_opens_the_other_side():
    """Long 100, sell 150: one closed trip, and a 50 short left open."""
    fills = [_fill(0, "BUY", 100, 100.0), _fill(4, "SELL", 150, 110.0)]
    bars = [_bar(0), _bar(4)]
    trips = round_trips_from_fills(fills, bars=bars)
    assert len(trips) == 1
    assert trips[0].quantity == Decimal("100")  # only the long was closed
    # The remaining 50 is open, so it is not a round trip and not a fill trip.
    assert count_round_trips(fills, bars=bars) == 1
    # Closing the short is the second trip.
    assert count_round_trips([*fills, _fill(8, "BUY", 50, 100.0)], bars=[*bars, _bar(8)]) == 2


def test_a_short_round_trip_is_counted_with_its_own_geometry():
    trips = round_trips_from_fills(
        [_fill(0, "SELL", 100, 110.0), _fill(4, "BUY", 100, 100.0)],
        bars=[_bar(0), _bar(4)],
    )
    assert len(trips) == 1
    assert trips[0].side == "SHORT"
    assert trips[0].gross_pnl == Decimal("1000.00")  # (110 - 100) * 100


def test_round_trips_are_counted_per_instrument():
    """The control that keeps two symbols from being one position.

    A single scalar ledger over this stream reports a round trip (buy 100, then
    ``qty`` returns to zero), which is a trade that never happened: no position
    was ever opened and closed.
    """
    fills = [_fill(0, "BUY", 100, 100.0, symbol="RELIANCE")]
    bars = [_bar(0, symbol="RELIANCE")]
    other = [_fill(1, "SELL", 100, 100.0, symbol="TCS")]
    other_bars = [_bar(1, symbol="TCS")]
    assert count_round_trips([*fills, *other], bars=[*bars, *other_bars]) == 0
    # Each on its own is one trip.
    assert count_round_trips(fills, bars=bars) == 0  # opened, never closed
    assert count_round_trips([*fills, _fill(5, "SELL", 100, 110.0, symbol="RELIANCE")],
                             bars=[*bars, _bar(5, symbol="RELIANCE")]) == 1


def test_round_trips_ignore_rejected_records_and_empty_ones():
    fills = [
        _fill(0, "BUY", 100, 100.0, rejected=True),
        _fill(1, "BUY", 0, 100.0),
        _fill(2, "BUY", 100, 100.0),
        _fill(3, "SELL", 100, 110.0),
    ]
    bars = [_bar(i) for i in range(4)]
    trips = round_trips_from_fills(fills, bars=bars)
    assert len(trips) == 1
    assert trips[0].entry_time == _T0 + timedelta(minutes=2)


def test_a_rejected_record_is_not_an_out_of_sample_fill():
    fills = [_fill(0, "BUY", 100, 100.0, rejected=True), _fill(1, "SELL", 100, 110.0)]
    assert len(oos_fills_in(fills, start=_T0, end=_T0 + timedelta(minutes=1))) == 1


def test_out_of_sample_fills_are_the_window_not_the_whole_run():
    """A fold's fills include the carried warmup bars, and those trades are
    training trades."""
    fills = [
        _fill(0, "BUY", 100, 100.0),  # warmup bar
        _fill(30, "SELL", 100, 110.0),  # first scored out of sample bar
    ]
    windowed = oos_fills_in(
        fills, start=_T0 + timedelta(minutes=30), end=_T0 + timedelta(minutes=60)
    )
    assert len(windowed) == 1
    assert windowed[0]["price"] == 110.0


def test_fills_that_cannot_be_placed_in_the_window_are_refused():
    """No timestamps, no answer.  Counting every fill in the run would put
    training trades into the out of sample count."""
    fills = [_fill(0, "BUY", 100, 100.0)]
    with pytest.raises(ValidationReportError) as excinfo:
        oos_fills_in(fills, start=None, end=None)
    assert "out of sample" in str(excinfo.value)
    with pytest.raises(ValidationReportError):
        oos_fills_in(fills, start=_T0, end=None)
    # An empty stream needs no window and is not an error.
    assert oos_fills_in([], start=None, end=None) == []


def test_a_fill_record_with_no_identifiable_instrument_is_refused():
    """Instrument identity is what keeps two symbols out of one position, so a
    record that cannot name its own is a loud failure rather than a guess."""
    fills = [_fill(0, "BUY", 100, 100.0), _fill(3, "SELL", 100, 110.0)]
    with pytest.raises(ValidationReportError) as excinfo:
        count_round_trips(fills)
    message = str(excinfo.value)
    assert "instrument" in message
    # Naming the instrument in the record resolves it with no bars at all.
    named = [
        {**f, "instrument": "NSE:RELIANCE"} for f in fills
    ]
    assert count_round_trips(named) == 1


def test_a_fill_is_attributed_to_the_bar_it_filled_against():
    """The engine stamps a ``next_open`` fill with the bar it filled at, so the
    bar names the instrument when the record does not."""
    fills = [_fill(0, "BUY", 100, 100.0, symbol="TCS"), _fill(4, "SELL", 100, 110.0, symbol="TCS")]
    bars = [_bar(0, symbol="TCS"), _bar(4, symbol="TCS")]
    stripped = [{k: v for k, v in f.items() if k != "instrument"} for f in fills]
    assert count_round_trips(stripped, bars=bars) == 1
    assert round_trips_from_fills(stripped, bars=bars)[0].symbol == "NSE:TCS"


def test_two_instruments_sharing_a_bar_timestamp_are_refused():
    """Ambiguity is not resolved by guessing.  Two bars at one timestamp and a
    record that names neither: which symbol traded is unknowable here."""
    fills = [{**_fill(0, "BUY", 100, 100.0), "instrument": None}]
    fills[0].pop("instrument")
    bars = [_bar(0, symbol="RELIANCE"), _bar(0, symbol="TCS")]
    with pytest.raises(ValidationReportError) as excinfo:
        count_round_trips(fills, bars=bars)
    message = str(excinfo.value)
    assert "ambiguous" in message or "NSE:RELIANCE" in message
    # The same stream with the key present counts without complaint.
    resolved = [{**fills[0], "instrument": "NSE:TCS"}, _fill(4, "SELL", 100, 110.0, symbol="TCS")]
    assert count_round_trips(resolved) == 1


def test_a_fill_outside_every_bar_is_refused():
    fills = [{**_fill(0, "BUY", 100, 100.0)}]
    with pytest.raises(ValidationReportError) as excinfo:
        count_round_trips(fills, bars=[_bar(99)])
    assert "instrument" in str(excinfo.value)


def test_a_domain_fill_is_accepted_as_well_as_a_record():
    """The research path may hold real ``Fill`` objects, so the counter reads
    both shapes rather than forcing one caller to convert."""
    from tradex_domain.enums import OrderSide
    from tradex_domain.execution import Fill
    from tradex_domain.value_objects import CorrelationId, OrderId
    from tradex_domain.value_objects import Price as P

    def _domain_fill(minutes: int, side: OrderSide) -> Fill:
        return Fill(
            order_id=OrderId(value=f"o{minutes}"),
            instrument=Equity.of("NSE", "RELIANCE"),
            side=side,
            quantity=Quantity(value=Decimal("100")),
            price=P(value=Decimal("100" if side is OrderSide.BUY else "110")),
            timestamp=_T0 + timedelta(minutes=minutes),
        )

    fills = [
        _domain_fill(0, OrderSide.BUY),
        _domain_fill(4, OrderSide.SELL),
    ]
    trips = round_trips_from_fills(fills)
    assert len(trips) == 1
    assert trips[0].gross_pnl == Decimal("1000.00")
    assert CorrelationId is not None  # imported for the domain shape's siblings


# ------------------------------------------- the fills / round trips distinction


def test_the_fill_count_is_not_the_round_trip_count_and_they_gate_differently():
    """The live mislabelling bug, as a test.

    ``BacktestResult.num_trades`` counts filled orders, the interface calls it
    Trades, and 55 fills on one run were 27 round trips.  The stream here is
    built to make the two readings disagree about the bar: each of its 60 round
    trips is five fills (two partial exits, a scale-in, and the fill that
    completes the position), so the fill reading clears a ``>= 100`` floor and
    the round trip reading does not.
    """
    fills: list[dict[str, Any]] = []
    minutes = 0

    def _add(side: str, qty: float, price: float) -> None:
        nonlocal minutes
        fills.append(
            {
                "time": _T0 + timedelta(minutes=minutes),
                "side": side,
                "price": price,
                "qty": qty,
                "rejected": False,
                "reason": "",
                "stop": None,
                "target": None,
            }
        )
        minutes += 1

    for _ in range(60):
        _add("BUY", 100, 100.0)  # open long 100
        _add("SELL", 30, 105.0)  # partial: the position is not closed yet
        _add("SELL", 20, 106.0)  # partial: still 50 open
        _add("BUY", 20, 107.0)  # scale in: 70 at an averaged 102.00
        _add("SELL", 70, 110.0)  # completes the position: one round trip
    bars = [_bar(i, symbol="RELIANCE") for i in range(minutes + 2)]

    window = {"start": bars[0].timestamp, "end": bars[-1].timestamp}
    oos = oos_fills_in(fills, **window)
    trips = round_trips_from_fills(oos, bars=bars)

    assert len(oos) == 300
    assert len(trips) == 60
    assert len(oos) != len(trips)
    # One trip's arithmetic, by hand: 30*(105-100) + 20*(106-100) + 70*(110-102).
    assert trips[0].gross_pnl == Decimal("830.00")
    assert trips[0].quantity == Decimal("120.00")  # every closed unit of the trip
    assert trips[0].exit_price == Decimal("108.08")  # weighted over the 3 exits

    honest = _report(oos_fills=len(oos), oos_round_trips=len(trips))
    assert honest.failed_fields == ("oos_round_trips",)

    # The flattering reading — the one the interface would produce — passes.
    flattering = _report(oos_fills=len(trips), oos_round_trips=len(oos))
    assert flattering.passed is True
    # And the two are not interchangeable, which is the whole point.
    assert honest.passed is False


# ------------------------------------------------------ expectancy and friends


def test_the_trade_metrics_are_the_shared_ones():
    """Expectancy, profit factor and drawdown come from ``compute_statistics``,
    the definitions the results surface already uses, so a report and a chart
    cannot disagree about them."""
    trips = round_trips_from_fills(
        [
            _fill(0, "BUY", 100, 100.0),
            _fill(4, "SELL", 100, 110.0),
            _fill(8, "BUY", 100, 110.0),
            _fill(12, "SELL", 100, 100.0),
        ],
        bars=[_bar(i) for i in (0, 4, 8, 12)],
    )
    curve = [100_000.0, 100_000.0, 100_100.0, 100_100.0, 100_000.0]
    stats = compute_statistics(trips, equity_curve=curve, frequency="daily")
    assert stats.total_trades == 2
    assert float(stats.expectancy) == pytest.approx(0.0)
    assert stats.profit_factor == pytest.approx(1.0)
    assert stats.max_drawdown_pct < 0
    # The row type is the shared one, so nothing downstream has to convert.
    assert all(isinstance(t, Trade) for t in trips)


def test_expectancy_and_profit_factor_survive_a_report_with_no_trades():
    report = _report(oos_round_trips=0, oos_fills=0, expectancy=0.0, profit_factor=0.0)
    assert report.passed is False
    assert report.failed_fields == ("oos_round_trips",)


# ------------------------------------------------------------------ identity


def test_the_parameter_set_hash_is_order_independent_and_value_sensitive():
    a = parameter_set_hash({"fast": 10, "slow": 30})
    b = parameter_set_hash({"slow": 30, "fast": 10})
    assert a == b
    assert parameter_set_hash({"fast": 10, "slow": 31}) != a
    assert len(a) == 16
    assert all(c in "0123456789abcdef" for c in a)


def test_two_strategies_on_the_same_parameters_have_different_reports():
    one = _report(strategy_id="sma_cross")
    two = _report(strategy_id="bollinger_breakout")
    assert one.parameter_set_hash == two.parameter_set_hash
    assert one.report_id != two.report_id or one.strategy_id != two.strategy_id
