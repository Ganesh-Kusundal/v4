"""The research entry point that produces a validation report (AC-13, lane 1g).

This is the *only* place a ``TradeValidationReport`` is created, and the file
carries the proof: an HTTP route cannot reach it, a mean of per fold Sharpes
cannot enter the report even when the per fold numbers are handed to it on a
plate, and a fill count cannot stand in for a round trip count.

The red state of each test is recorded in ``.superpowers/sdd/task-A4-report.md``.
"""

from __future__ import annotations

import ast
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tradex_analytics.validation_report import TradeValidationReport
from tradex_domain import OHLC, Candle, Price, Quantity, Timeframe
from tradex_domain.instruments import Equity
from tradex_execution.validation_report_store import ValidationReportStore
from tradex_replay.backtest import BacktestResult
from tradex_replay.validation import (
    COST_STRESS_MULTIPLIER,
    ValidationRunError,
    generate_validation_report,
    generate_validation_report_by_date,
)

_ROOT = Path(__file__).resolve().parents[3]

# --------------------------------------------------------------------------- fixtures

_DAY0 = datetime(2026, 1, 5, 9, 15)

#: A non-constant daily return pattern.  A constant one has zero variance, so
#: every Sharpe taken against it would be 0.0 and prove nothing.
_PATTERN = (0.004, -0.002, 0.001, 0.003, -0.001, 0.002, 0.0005, -0.003)

#: 300 trading days of D1 candles, one per day.
_DAYS = 300


def _candles(days: int = _DAYS) -> list[Candle]:
    out = []
    for day in range(days):
        p = Price(value=Decimal("100"))
        out.append(
            Candle(
                instrument=Equity.of("NSE", "RELIANCE"),
                timeframe=Timeframe.D1,
                ohlc=OHLC(open=p, high=p, low=p, close=p),
                volume=Quantity(value=Decimal("1000")),
                timestamp=_DAY0 + timedelta(days=day),
            )
        )
    return out


def _curve(n_bars: int, *, rotation: int = 0, drift: float = 0.0) -> list[float]:
    """A bar-aligned equity curve: one leading point, then one per bar.

    The engine seeds the curve with the initial capital and appends one
    mark-to-market point per candle, so ``len(curve) == bars + 1``.
    """
    out = [100_000.0]
    for i in range(n_bars):
        out.append(out[-1] * (1 + _PATTERN[(i + rotation) % len(_PATTERN)] + drift))
    return out


def _round_trip_run(
    bars: list[Candle],
    *,
    rotation: int = 0,
    drift: float = 0.0,
    sharpe_attr: float | None = None,
    instrument_key: bool = False,
) -> BacktestResult:
    """A run that opens a position every second bar and closes it on the next.

    The pairing is what makes the numbers checkable by hand: a 61 bar out of
    sample window (21 warmup + 1 embargo + 39 scored) yields entries on the
    even indices, so the scored window holds 19 complete round trips and 39
    executed fills — two different numbers, which is the point.
    """
    fills: list[dict[str, Any]] = []
    for i, bar in enumerate(bars):
        if i % 2:
            continue
        entry_price = 100.0 + i
        fills.append(
            {
                "time": bar.timestamp,
                "side": "BUY",
                "price": entry_price,
                "qty": 100.0,
                "rejected": False,
                "reason": "",
                "stop": None,
                "target": None,
            }
        )
        if i + 1 < len(bars):
            fills.append(
                {
                    "time": bars[i + 1].timestamp,
                    "side": "SELL",
                    "price": entry_price + 10.0,
                    "qty": 100.0,
                    "rejected": False,
                    "reason": "",
                    "stop": None,
                    "target": None,
                }
            )
    if instrument_key:
        for record in fills:
            record["instrument"] = "NSE:RELIANCE"
    curve = _curve(len(bars), rotation=rotation, drift=drift)
    return BacktestResult(
        total_return=curve[-1] / curve[0] - 1.0,
        sharpe=sharpe_attr if sharpe_attr is not None else 1.5,
        max_drawdown=-0.05,
        num_trades=len(fills),
        equity_curve=curve,
        fills=fills,
    )


def _flat_run(bars: list[Candle], *, sharpe_attr: float = 9.0) -> BacktestResult:
    """A run whose curve is flat but whose per run ``sharpe`` field is 9.0.

    This is the tempting shortcut handed over on a plate: the per fold Sharpe
    says the strategy is excellent and the equity says it is not.
    """
    curve = [100_000.0] * (len(bars) + 1)
    return BacktestResult(
        total_return=0.0,
        sharpe=sharpe_attr,
        max_drawdown=0.0,
        num_trades=0,
        equity_curve=curve,
        fills=[],
    )


def _stress_run(bars: list[Candle], *, per_bar: float = 0.0005) -> BacktestResult:
    """The rerun at doubled costs: same shape, a fraction of the edge per bar."""
    curve = [100_000.0]
    for _ in bars:
        curve.append(curve[-1] * (1 + per_bar))
    return BacktestResult(
        total_return=curve[-1] / curve[0] - 1.0,
        sharpe=1.1,
        max_drawdown=-0.01,
        num_trades=0,
        equity_curve=curve,
        fills=[],
    )


def _stress_run_losing(bars: list[Candle]) -> BacktestResult:
    curve = [100_000.0]
    for _ in bars:
        curve.append(curve[-1] * 0.999)
    return BacktestResult(
        total_return=-0.001,
        sharpe=-1.0,
        max_drawdown=-0.05,
        num_trades=0,
        equity_curve=curve,
        fills=[],
    )


_GENEROUS = {
    "deflated_sharpe": 0.42,
    "param_stability_share_of_grid": 0.71,
}


def _report(
    *,
    run_fn=_round_trip_run,
    stress_run_fn=_stress_run,
    store=None,
    instrument_key: bool = False,
    **kwargs: Any,
) -> TradeValidationReport:
    options: dict[str, Any] = {
        "strategy_id": "sma_cross",
        "parameters": {"fast": 10, "slow": 30},
        "run_fn": lambda params, bars: run_fn(bars, instrument_key=instrument_key)
        if run_fn is _round_trip_run
        else run_fn(bars),
        "optimize_fn": lambda bars: {"fast": 10, "slow": 30},
        "stress_run_fn": lambda params, bars: stress_run_fn(bars),
        "train_days": 60,
        "test_days": 40,
        "warmup_bars": 21,
        "store": store,
        **_GENEROUS,
    }
    options.update(kwargs)
    return generate_validation_report_by_date(_candles(), **options)


# ------------------------------------------------------------------ the six criteria


def test_a_strategy_that_clears_the_bar_gets_a_passing_report():
    """The whole flow, end to end, on synthetic daily data."""
    report = _report()
    assert isinstance(report, TradeValidationReport)
    assert report.strategy_id == "sma_cross"
    assert report.bar_frequency == "1d"
    assert report.oos_sharpe >= 1.0
    assert report.oos_round_trips >= 100
    assert report.oos_span_days >= 90.0
    assert report.net_pnl_at_2x_costs > 0
    assert report.survives_2x_costs is True
    assert report.deflated_sharpe == pytest.approx(0.42)
    assert report.param_stability_share_of_grid == pytest.approx(0.71)
    assert report.passed is True
    assert report.failures == ()
    assert report.promotion_status == "passed"


def test_the_report_counts_round_trips_and_keeps_the_fill_count_apart():
    """The numbers, by hand: 6 folds x (19 round trips, 39 fills).

    39 - 21 warmup - 1 embargo = 39 scored bars... the window is 40 test days
    with one embargo bar, so 39 scored.  Entries sit on even indices of the
    run's input, so the 22nd bar (index 21) is the embargo and index 22 is the
    first scored bar: 19 complete trips and 39 executed fills inside it.
    """
    report = _report()
    assert report.oos_round_trips == 6 * 19
    assert report.oos_fills == 6 * 39
    assert report.oos_fills != report.oos_round_trips


def test_a_fewer_than_a_hundred_round_trips_does_not_pass():
    report = _report()
    assert report.oos_round_trips >= 100  # the control, before narrowing it

    short = generate_validation_report_by_date(
        _candles(200),
        strategy_id="sma_cross",
        parameters={"fast": 10},
        run_fn=lambda params, bars: _round_trip_run(bars),
        optimize_fn=lambda bars: {},
        stress_run_fn=lambda params, bars: _stress_run(bars),
        train_days=60,
        test_days=40,
        **_GENEROUS,
    )
    assert short.oos_round_trips < 100
    assert short.failed_fields == ("oos_round_trips",)
    assert short.passed is False


def test_a_short_out_of_sample_span_does_not_pass():
    """Two folds over 150 days: a 79 day span, and every other criterion met.

    Each fold is judged on 39 bars, comfortably over lane 1d's floor of 30, so
    this is criterion three alone failing — a strategy can have a good Sharpe and
    plenty of round trips and still have been traded for two months.
    """
    report = generate_validation_report_by_date(
        _candles(150),  # 2 folds, first scored bar at day 61, last at day 139
        strategy_id="sma_cross",
        parameters={"fast": 10},
        run_fn=lambda params, bars: _round_trip_run(bars),
        optimize_fn=lambda bars: {},
        stress_run_fn=lambda params, bars: _stress_run(bars),
        train_days=60,
        test_days=40,
        **_GENEROUS,
    )
    assert 60 < report.oos_span_days < 90
    assert report.oos_round_trips > 0
    # This window is short on two counts, and the span is the one under test.
    # (One criterion failing in isolation is the report module's test: the six
    # are walked one at a time there.)
    assert "oos_span_days" in report.failed_fields
    assert report.passed is False


def test_a_negative_deflated_sharpe_does_not_pass():
    report = _report(deflated_sharpe=-0.01)
    assert report.failed_fields == ("deflated_sharpe",)
    assert report.passed is False


def test_a_spike_rather_than_a_plateau_does_not_pass():
    report = _report(param_stability_share_of_grid=0.2)
    assert report.failed_fields == ("param_stability_share_of_grid",)
    assert report.passed is False


# ------------------------------------------------- criterion 1, the shortcut


def test_the_mean_of_per_fold_sharpes_cannot_enter_the_report():
    """The failure the whole programme exists to remove, as a hostile input.

    Every fold's ``BacktestResult.sharpe`` is 9.0, so the mean of per fold
    Sharpes clears ``oos_sharpe >= 1.0`` by a mile.  The equity curves are flat,
    so the Sharpe of the concatenated out of sample series is 0.0 and the report
    must fail.  If the report ever read ``step.out_of_sample_result.sharpe``,
    this test would pass a strategy that did nothing.
    """
    report = _report(run_fn=_flat_run)
    fold_sharpes = [step.out_of_sample_result.sharpe for step in _steps()]
    assert fold_sharpes and all(s == 9.0 for s in fold_sharpes)
    assert sum(fold_sharpes) / len(fold_sharpes) >= 1.0  # the flattering number
    assert report.oos_sharpe == pytest.approx(0.0)  # the true one
    assert "oos_sharpe" in report.failed_fields
    assert report.passed is False
    # A run that did nothing also fails the trade count; the point here is only
    # that the Sharpe failure is present, and that a 9.0 per fold Sharpe did not
    # buy its way out of it.
    assert report.oos_round_trips == 0


def _steps():
    from tradex_replay.walk_forward import run_walk_forward_by_date

    return run_walk_forward_by_date(
        _candles(),
        lambda params, bars: _flat_run(bars),
        lambda bars: {},
        train_days=60,
        test_days=40,
        warmup_bars=21,
    ).steps


def test_the_report_sharpe_is_the_concatenated_series_annualised_at_the_bar_frequency():
    """Computed on the stitched out of sample curve, at the stored frequency."""
    from tradex_analytics.reports import sharpe_ratio
    from tradex_replay.walk_forward import run_walk_forward_by_date

    walk = run_walk_forward_by_date(
        _candles(),
        lambda params, bars: _round_trip_run(bars),
        lambda bars: {},
        train_days=60,
        test_days=40,
        warmup_bars=21,
    )
    curve = walk.oos_equity_curve
    returns = [
        (curve[i] - curve[i - 1]) / curve[i - 1]
        for i in range(1, len(curve))
        if curve[i - 1]
    ]
    report = _report()
    assert report.oos_sharpe == pytest.approx(sharpe_ratio(returns, 0.0, "1d"))
    assert report.oos_sharpe == pytest.approx(walk.aggregate_oos_sharpe)


# ----------------------------------------------------- criterion 4, the cost stress


def test_the_cost_stress_reruns_the_out_of_sample_segments_and_nothing_else():
    """Exactly the spec's words: *rerun the out of sample segments*.

    The stress run sees each fold's warmup + embargo + scored window, at the
    parameters the fold chose, and never the training window — re-optimising at
    doubled costs would grade a different strategy.
    """
    seen: list[tuple[dict[str, Any], int, Any]] = []

    def _spy(params: dict[str, Any], bars: list[Candle]) -> BacktestResult:
        seen.append((params, len(bars), bars[0].timestamp))
        return _stress_run(bars)

    generate_validation_report_by_date(
        _candles(),
        strategy_id="sma_cross",
        parameters={"fast": 10, "slow": 30},
        run_fn=lambda params, bars: _round_trip_run(bars),
        optimize_fn=lambda bars: {"fast": 10, "slow": 30},
        stress_run_fn=_spy,
        train_days=60,
        test_days=40,
        warmup_bars=21,
        **_GENEROUS,
    )
    assert len(seen) == 6
    for fold, (params, n_bars, first_bar) in enumerate(seen):
        assert params == {"fast": 10, "slow": 30}
        assert n_bars == 21 + 1 + 39, "warmup + embargo + scored"
        # The warmup is the *tail* of that fold's training window: fold f trains
        # on days 40f..40f+59, so its warmup starts at 40f+39 and not at 40f.
        assert first_bar == _DAY0 + timedelta(days=fold * 40 + 39)


def test_a_strategy_that_does_not_survive_doubled_costs_does_not_pass():
    report = _report(stress_run_fn=_stress_run_losing)
    assert report.net_pnl_at_2x_costs < 0
    assert report.survives_2x_costs is False
    assert report.failed_fields == ("net_pnl_at_2x_costs",)
    assert report.passed is False


def test_the_cost_stress_multiplier_is_two():
    assert COST_STRESS_MULTIPLIER == 2.0


def test_the_stress_run_covering_different_segments_is_refused():
    """The stress P&L is only comparable if it was measured on the same
    segments.  A window change between the two runs would silently grade a
    different data set."""
    calls = {"n": 0}

    def _shifting_run(params: dict[str, Any], bars: list[Candle]) -> BacktestResult:
        calls["n"] += 1
        # Change the window shape on the second fold.
        return _stress_run(bars if calls["n"] != 2 else bars[:-1])

    with pytest.raises(ValidationRunError) as excinfo:
        generate_validation_report_by_date(
            _candles(),
            strategy_id="sma_cross",
            parameters={"fast": 10, "slow": 30},
            run_fn=lambda params, bars: _round_trip_run(bars),
            optimize_fn=lambda bars: {},
            stress_run_fn=_shifting_run,
            train_days=60,
            test_days=40,
            warmup_bars=21,
            **_GENEROUS,
        )
    assert "fold 1" in str(excinfo.value)


# ---------------------------------------------------- the inputs it will not invent


def test_a_report_cannot_be_built_without_the_deflated_sharpe():
    with pytest.raises(ValidationRunError) as excinfo:
        generate_validation_report_by_date(
            _candles(),
            strategy_id="sma_cross",
            parameters={"fast": 10},
            run_fn=lambda params, bars: _round_trip_run(bars),
            optimize_fn=lambda bars: {},
            stress_run_fn=lambda params, bars: _stress_run(bars),
            deflated_sharpe=None,
            param_stability_share_of_grid=0.71,
            train_days=60,
            test_days=40,
        )
    message = str(excinfo.value)
    assert "deflated_sharpe" in message
    assert "GridSearchResult" in message


def test_a_report_cannot_be_built_without_the_parameter_stability_share():
    with pytest.raises(ValidationRunError) as excinfo:
        generate_validation_report_by_date(
            _candles(),
            strategy_id="sma_cross",
            parameters={"fast": 10},
            run_fn=lambda params, bars: _round_trip_run(bars),
            optimize_fn=lambda bars: {},
            stress_run_fn=lambda params, bars: _stress_run(bars),
            deflated_sharpe=0.42,
            param_stability_share_of_grid=None,
            train_days=60,
            test_days=40,
        )
    assert "param_stability_share_of_grid" in str(excinfo.value)


def test_a_report_cannot_be_built_without_a_cost_stress_run():
    """Criterion four is a rerun.  Omitting the rerun would leave the field at a
    value nothing produced."""
    with pytest.raises(ValidationRunError) as excinfo:
        generate_validation_report_by_date(
            _candles(),
            strategy_id="sma_cross",
            parameters={"fast": 10},
            run_fn=lambda params, bars: _round_trip_run(bars),
            optimize_fn=lambda bars: {},
            stress_run_fn=None,
            deflated_sharpe=0.42,
            param_stability_share_of_grid=0.71,
            train_days=60,
            test_days=40,
        )
    assert "cost" in str(excinfo.value).lower()


def test_a_fold_below_thirty_out_of_sample_bars_fails_the_report():
    """Lane 1d's floor is armed on this path, not bypassed by it.

    The entry point does not re-check the floor — the fold does — so the proof
    is that a window too short to be judged stops the report before a verdict
    exists.
    """
    with pytest.raises(Exception) as excinfo:
        generate_validation_report_by_date(
            _candles(120),
            strategy_id="sma_cross",
            parameters={"fast": 10},
            run_fn=lambda params, bars: _round_trip_run(bars),
            optimize_fn=lambda bars: {},
            stress_run_fn=lambda params, bars: _stress_run(bars),
            train_days=60,
            test_days=20,  # 20 - 1 embargo = 19 scored bars
            **_GENEROUS,
        )
    assert "30" in str(excinfo.value)


# ------------------------------------------------------------------- round trips


def test_a_position_opened_in_the_warmup_is_not_an_out_of_sample_round_trip():
    """The carried warmup bars are training data.

    A run that opens on the first warmup bar and closes on the first scored bar
    has one out of sample fill and no out of sample round trip: the entry was a
    training decision, so the exit is an orphan rather than the close of a trade.
    Counting the pair would inflate the criterion's floor with a trade the
    strategy did not take out of sample.
    """
    def _warmup_entry(bars: list[Candle]) -> BacktestResult:
        fills = [
            {
                "time": bars[0].timestamp,
                "side": "BUY",
                "price": 100.0,
                "qty": 100.0,
                "rejected": False,
                "reason": "",
                "stop": None,
                "target": None,
            },
            {
                "time": bars[22].timestamp,
                "side": "SELL",
                "price": 110.0,
                "qty": 100.0,
                "rejected": False,
                "reason": "",
                "stop": None,
                "target": None,
            },
        ]
        return BacktestResult(
            total_return=0.1,
            sharpe=1.5,
            max_drawdown=-0.02,
            num_trades=2,
            equity_curve=_curve(len(bars)),
            fills=fills,
        )

    report = generate_validation_report_by_date(
        _candles(140),
        strategy_id="sma_cross",
        parameters={"fast": 10},
        run_fn=lambda params, bars: _warmup_entry(bars),
        optimize_fn=lambda bars: {},
        stress_run_fn=lambda params, bars: _stress_run(bars),
        train_days=60,
        test_days=40,
        warmup_bars=21,
        **_GENEROUS,
    )
    # Two folds, one out of sample fill each: the exit, at the first scored bar.
    # The entry is on a warmup bar and is not in the window at all, so nothing
    # pairs with it and the count is zero.
    assert report.oos_fills == 2
    assert report.oos_round_trips == 0
    assert "oos_round_trips" in report.failed_fields


def test_a_position_open_at_a_fold_boundary_is_not_a_round_trip():
    """Each fold is an independent run with its own book.

    A buy in fold 0 and a sell in fold 1 are two fills of two runs, not one round
    trip: pairing across the boundary would invent a trade that no single run
    made, and it would make the count larger than the runs support.
    """
    def _one_side(side: str):
        def _run(bars: list[Candle]) -> BacktestResult:
            return BacktestResult(
                total_return=0.0,
                sharpe=0.0,
                max_drawdown=0.0,
                num_trades=1,
                equity_curve=[100_000.0] * (len(bars) + 1),
                fills=[
                    {
                        "time": bars[22].timestamp,
                        "side": side,
                        "price": 100.0,
                        "qty": 100.0,
                        "rejected": False,
                        "reason": "",
                        "stop": None,
                        "target": None,
                    }
                ],
            )

        return _run

    # Two folds, each run twice: in-sample then out of sample.  Both folds of
    # fold 0 open a long, both folds of fold 1 close one.
    sides = iter([_one_side("BUY"), _one_side("BUY"), _one_side("SELL"), _one_side("SELL")])
    report = generate_validation_report_by_date(
        _candles(140),
        strategy_id="sma_cross",
        parameters={"fast": 10},
        run_fn=lambda params, bars: next(sides)(bars),
        optimize_fn=lambda bars: {},
        stress_run_fn=lambda params, bars: _stress_run(bars),
        train_days=60,
        test_days=40,
        warmup_bars=21,
        **_GENEROUS,
    )
    assert report.oos_fills == 2
    assert report.oos_round_trips == 0
    assert "oos_round_trips" in report.failed_fields


def test_fills_are_attributed_to_the_bar_they_filled_against():
    """The production record shape carries no instrument, and the pairing still
    has to know which position it is closing."""
    report = _report(instrument_key=False)
    assert report.oos_round_trips == 6 * 19
    named = _report(instrument_key=True)
    assert named.oos_round_trips == report.oos_round_trips
    assert named.oos_fills == report.oos_fills


# ------------------------------------------------------------------- persistence


def test_the_report_is_persisted_under_its_strategy_and_parameter_set(tmp_path):
    from tradex_analytics.validation_report import parameter_set_hash

    store = ValidationReportStore(tmp_path / "state.db")
    try:
        report = _report(store=store)
        row = store.get("sma_cross", report.parameter_set_hash)
        assert row is not None
        assert row.parameter_set_hash == parameter_set_hash({"fast": 10, "slow": 30})
        assert row.stored_passed is True
        assert store.promotion_status("sma_cross") == "passed"
        assert store.promotion_status("bollinger_breakout") == "not_evaluated"
        assert store.is_promotable("bollinger_breakout") is False
    finally:
        store.close()


def test_a_failing_report_is_persisted_with_its_failures(tmp_path):
    store = ValidationReportStore(tmp_path / "state.db")
    try:
        report = _report(store=store, param_stability_share_of_grid=0.1)
        row = store.get("sma_cross", report.parameter_set_hash)
        assert row is not None
        assert row.stored_passed is False
        # The stored failures are the reasons, not bare field names: this is what
        # the safety surface shows (AC-13).
        assert len(row.stored_failures) == 1
        assert "param_stability_share_of_grid" in row.stored_failures[0]
        # And the row rebuilds into a report that agrees with the verdict.
        rebuilt = TradeValidationReport(**row.as_report_kwargs())
        assert rebuilt.passed is False
        assert rebuilt.failed_fields == ("param_stability_share_of_grid",)
    finally:
        store.close()


def test_a_tampered_row_cannot_launder_a_promotion(tmp_path):
    """``passed = 1`` on a row whose metrics fail the bar rebuilds as a failure.

    The verdict is recomputed from the metrics on the read path, so the only way
    to change a verdict is to change a number — which is the audit trail this
    whole programme is for.
    """

    store = ValidationReportStore(tmp_path / "state.db")
    try:
        report = _report(store=store)
        store.connection().execute(
            "UPDATE tradex_validation_report SET passed = 1, oos_sharpe = 0.1"
        )
        store.connection().commit()
        row = store.get("sma_cross", report.parameter_set_hash)
        assert row is not None
        rebuilt = TradeValidationReport(**row.as_report_kwargs())
        assert rebuilt.oos_sharpe == pytest.approx(0.1)
        assert rebuilt.passed is False
        assert rebuilt.failed_fields == ("oos_sharpe",)
    finally:
        store.close()


# ------------------------------------------------- not reachable from a route


def _imported_modules(source: str) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
    return modules


def _calls_named(source: str, *names: str) -> set[str]:
    called: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            called.add(func.id)
        elif isinstance(func, ast.Attribute):
            called.add(func.attr)
    return {n for n in names if n in called}


_ROUTES = _ROOT / "interfaces/src/tradex_interfaces/routes"


def test_no_http_route_imports_the_report_entry_point():
    """A route is a presentation surface.

    If it could build the report, a crafted request could build a passing one,
    so the edge is refused structurally rather than by convention: nothing under
    ``interfaces/src`` may import the module the report is generated from.
    """
    offenders: list[str] = []
    for path in sorted((_ROOT / "interfaces/src").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        modules = _imported_modules(path.read_text(encoding="utf-8"))
        hits = sorted(
            m for m in modules
            if m.startswith("tradex_replay.validation")
            or m.startswith("tradex_analytics.validation_report")
        )
        if hits:
            offenders.append(f"{path.relative_to(_ROOT)}: {hits}")
    assert not offenders, "\n".join(offenders)


def test_no_http_route_calls_the_entry_point():
    """Belt and braces on the same property, by name rather than by import.

    A route that reached the generator through a re-export, a factory or a
    ``getattr`` would pass the import scan; this does not.
    """
    names = ("generate_validation_report", "generate_validation_report_by_date")
    offenders = [
        str(path.relative_to(_ROOT))
        for path in sorted(_ROUTES.rglob("*.py"))
        if "__pycache__" not in path.parts
        and _calls_named(path.read_text(encoding="utf-8"), *names)
    ]
    assert not offenders, offenders
    # The scan is looking at something.
    assert len(list(_ROUTES.glob("*.py"))) > 3


def test_the_entry_point_does_not_import_the_web_layer():
    """The research path is not a web path: it cannot serve a report, and the
    report does not need one to exist."""
    from tradex_replay import validation

    source = Path(validation.__file__).read_text(encoding="utf-8")
    forbidden = _imported_modules(source) & {"fastapi", "tradex_interfaces", "starlette"}
    assert not forbidden, sorted(forbidden)


def test_the_route_guards_are_not_vacuous():
    """Negative controls: the scanners above detect the thing they forbid."""
    offending = "from tradex_replay.validation import generate_validation_report\n"
    assert any(
        m.startswith("tradex_replay.validation")
        for m in _imported_modules(offending)
    )
    calling = "def run():\n    return generate_validation_report_by_date(data)\n"
    assert _calls_named(calling, "generate_validation_report_by_date")
    # And an unrelated import is left alone, or the guard would be switched off.
    assert not any(
        m.startswith("tradex_replay.validation")
        for m in _imported_modules("from tradex_replay.backtest import BacktestEngine\n")
    )


def test_the_only_writer_of_a_report_is_this_entry_point():
    """One writer, by construction of the search: no other module names
    ``TradeValidationReport(`` as a call.

    A second writer anywhere in the workspace is a second promotion decision,
    which is the thing the spec is removing.
    """
    writers: list[str] = []
    for path in sorted(_ROOT.rglob("*.py")):
        parts = path.parts
        if any(p in {"node_modules", "__pycache__", ".git", "openalgo-charts"} for p in parts):
            continue
        if path.name == "test_validation_report.py" and "analytics" in parts:
            continue
        if "tests" in parts or path.name.startswith("test_"):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):  # pragma: no cover - binary files
            continue
        if "TradeValidationReport(" in source and "deflated_sharpe" in source:
            writers.append(str(path.relative_to(_ROOT)))
    assert writers == ["replay/src/tradex_replay/validation.py"], writers


# ------------------------------------------------------------------ bar windows


def test_the_index_window_entry_point_produces_the_same_report_shape():
    """The bar indexed variant is the same report over bar windows."""
    report = generate_validation_report(
        _candles(),
        strategy_id="sma_cross",
        parameters={"fast": 10},
        run_fn=lambda params, bars: _round_trip_run(bars),
        optimize_fn=lambda bars: {},
        stress_run_fn=lambda params, bars: _stress_run(bars),
        train_window=60,
        test_window=40,
        step_size=40,
        warmup_bars=21,
        **_GENEROUS,
    )
    # Candles carry timestamps even in index mode, so the span is placeable and
    # criterion three is judged rather than skipped.
    assert report.oos_span_days >= 90.0
    assert report.oos_round_trips >= 100
    assert report.passed is True


def test_bars_with_no_timestamp_cannot_manufacture_a_span():
    """A span of ``0.0`` fails criterion three; it is not a number that skips it.

    Index mode over a plain sequence has no bar timestamps, so the report says
    the span cannot be placed and the verdict is a failure. Inventing a span
    from bar indices would be a criterion satisfied by nothing.
    """
    report = generate_validation_report(
        list(range(300)),
        strategy_id="sma_cross",
        parameters={"fast": 10},
        run_fn=lambda params, bars: BacktestResult(
            total_return=0.0,
            sharpe=1.5,
            max_drawdown=-0.01,
            num_trades=0,
            equity_curve=_curve(len(bars)),
        ),
        optimize_fn=lambda bars: {},
        stress_run_fn=lambda params, bars: _stress_run(bars),
        train_window=60,
        test_window=40,
        step_size=40,
        warmup_bars=21,
        **_GENEROUS,
    )
    assert report.oos_span_days == 0.0
    assert report.failed_fields == ("oos_round_trips", "oos_span_days")
    assert report.passed is False
