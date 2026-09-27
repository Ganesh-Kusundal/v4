"""Persistence for the validation report (AC-13, lane 1g; rehydration, B4).

The table lives in the same SQLite file as the event store, keyed
``(strategy_id, parameter_set_hash)``, and a strategy with no row reads as
``not_evaluated``.

Three properties are the reason this file exists rather than a report written to
JSON:

* the fill count and the round trip count are separate ``NOT NULL`` columns
  bound by name, so ``oos_fills`` can never arrive where ``oos_round_trips`` is
  expected;
* **the only way to get a report out of the store is to rehydrate one**, and
  rehydration recomputes the verdict, so a row edited to say ``passed = 1`` is
  read as the failure its metrics describe; and
* the store cannot be constructed at all without the rehydrator, so there is no
  configuration in which the recompute is optional.

The rehydrator is the *real* ``TradeValidationReport`` here, not a stand-in, so
the tamper test is end to end: a test-only import of ``tradex_analytics`` from
``execution/tests`` is the composition root, and it is the one place the two
packages are joined. ``execution/src`` never imports it.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from tradex_analytics.validation_report import (
    PROMOTION_CRITERIA,
    REPORT_FIELDS,
    TradeValidationReport,
)
from tradex_execution.validation_report_store import (
    REPORT_COLUMNS,
    ValidationReportRow,
    ValidationReportStore,
    ValidationReportStoreError,
)


def _rehydrate(kwargs: dict[str, Any]) -> TradeValidationReport:
    """The composition root's wiring, verbatim from the store's docstring.

    This one lambda is the whole reason a tampered row cannot promote a strategy.
    """
    return TradeValidationReport(**kwargs)


# --------------------------------------------------------------------------- fixtures


def _passing(**overrides: Any) -> TradeValidationReport:
    """A report that clears all six criteria, so overrides can break exactly one."""
    metrics: dict[str, Any] = {
        "report_id": "report-1",
        "strategy_id": "sma_cross",
        "parameter_set_hash": "0f1e2d3c4b5a6978",
        "oos_sharpe": 1.8,
        "oos_round_trips": 120,
        "oos_fills": 300,
        "oos_span_days": 210.0,
        "expectancy": 12.5,
        "profit_factor": 1.6,
        "max_drawdown_pct": -8.25,
        "deflated_sharpe": 0.42,
        "param_stability_share_of_grid": 0.71,
        "net_pnl_at_2x_costs": 180_000.0,
        "bar_frequency": "1m",
    }
    metrics.update(overrides)
    report = TradeValidationReport(**metrics)
    # Every metric override in this file breaks exactly one criterion and every
    # identity override breaks none, so this keeps the fixture honest rather than
    # letting a test that thinks it wrote a failing report write a passing one.
    identity = {"report_id", "strategy_id", "parameter_set_hash"}
    assert report.passed is identity.issuperset(overrides), "the fixture must mean what it says"
    return report


class _Report:
    """A duck-typed writer input, for the two refusal tests.

    Deliberately not a ``TradeValidationReport``: the store reads the fields it
    names and knows nothing about the verdict's provenance, so these pin that
    contract — a report missing a metric, or carrying one as ``None``, stops at
    the storage layer rather than becoming a row that reads as a verdict.
    """

    def __init__(self, **overrides: Any) -> None:
        self.report_id = "report-1"
        self.strategy_id = "sma_cross"
        self.parameter_set_hash = "0f1e2d3c4b5a6978"
        self.oos_sharpe = 1.8
        self.oos_round_trips = 120
        self.oos_fills = 300
        self.oos_span_days = 210.0
        self.expectancy = 12.5
        self.profit_factor = 1.6
        self.max_drawdown_pct = -8.25
        self.deflated_sharpe = 0.42
        self.param_stability_share_of_grid = 0.71
        self.net_pnl_at_2x_costs = 180_000.0
        self.bar_frequency = "1m"
        self.passed = True
        self.survives_2x_costs = True
        self.failures = ()
        for key, value in overrides.items():
            setattr(self, key, value)


@pytest.fixture
def store(tmp_path):
    store = ValidationReportStore(tmp_path / "state.db", rehydrate=_rehydrate)
    yield store
    store.close()


def _tamper(store: ValidationReportStore, **columns: Any) -> None:
    """Write *columns* straight over a stored row, as a file edit would."""
    assignments = ", ".join(f"{name} = ?" for name in columns)
    store.connection().execute(
        f"UPDATE tradex_validation_report SET {assignments} "  # noqa: S608 - test
        "WHERE strategy_id = ?",
        (*columns.values(), "sma_cross"),
    )
    store.connection().commit()


# -------------------------------------------------------------------------- the table


def test_the_table_is_created_and_keyed_by_strategy_and_parameter_set(store):
    store.save(_passing())
    store.save(_passing(report_id="report-2", parameter_set_hash="aaaa0000bbbb1111"))
    store.save(_passing(report_id="report-3", strategy_id="bollinger_breakout"))

    assert store.count() == 3
    assert store.get("sma_cross", "0f1e2d3c4b5a6978") is not None
    assert store.get("sma_cross", "aaaa0000bbbb1111") is not None
    assert store.get("bollinger_breakout", "0f1e2d3c4b5a6978") is not None
    # The key is the pair, so a report for a parameter set nobody asked about
    # does not answer for this one.
    assert store.get("sma_cross", "not-a-hash") is None


def test_the_same_parameter_set_is_replaced_not_accumulated(store):
    store.save(_passing())
    store.save(_passing(report_id="report-2", oos_sharpe=0.2))
    assert store.count() == 1
    report = store.get("sma_cross", "0f1e2d3c4b5a6978")
    assert report is not None
    assert report.oos_sharpe == pytest.approx(0.2)
    assert report.passed is False
    assert store.stored_row(
        "sma_cross", "0f1e2d3c4b5a6978"
    ).stored_passed is False


def test_every_metric_survives_the_round_trip(store):
    original = _passing()
    store.save(original)
    report = store.get("sma_cross", "0f1e2d3c4b5a6978")
    assert report is not None
    for name in REPORT_COLUMNS:
        if name in {"passed", "survives_2x_costs", "failures"}:
            continue  # derived on read, asserted by the verdict tests below
        assert getattr(report, name) == pytest.approx(getattr(original, name)), name
    assert report.passed == original.passed
    assert report.survives_2x_costs == original.survives_2x_costs


# ------------------------------------------------------------- fills vs round trips


def test_the_two_counts_are_separate_columns_and_are_bound_by_name(store):
    """The control for the live mislabelling bug, at the storage layer.

    ``oos_fills`` is 300 and ``oos_round_trips`` is 120 in the fixture: a store
    that bound them by position, or that had one column for both, would come
    back with the same number twice.
    """
    store.save(_passing())
    report = store.get("sma_cross", "0f1e2d3c4b5a6978")
    assert report is not None
    assert report.oos_fills == 300
    assert report.oos_round_trips == 120
    assert "oos_fills" in REPORT_COLUMNS
    assert "oos_round_trips" in REPORT_COLUMNS
    # Both are declared NOT NULL, so neither can be absent from a row.
    info = {
        row["name"]: (row["type"], bool(row["notnull"]), row["cid"])
        for row in store.connection().execute(
            "PRAGMA table_info(tradex_validation_report)"
        )
    }
    assert info["oos_round_trips"] == ("INTEGER", True, info["oos_round_trips"][2])
    assert info["oos_fills"][1] is True
    assert info["oos_round_trips"][2] < info["oos_fills"][2], (
        "oos_round_trips is declared first so a positional bind cannot swap them"
    )


def test_a_report_missing_a_field_is_refused_by_name(store):
    class _Partial:
        report_id = "report-x"
        strategy_id = "sma_cross"
        parameter_set_hash = "hash"

    with pytest.raises(ValidationReportStoreError) as excinfo:
        store.save(_Partial())  # type: ignore[arg-type]
    message = str(excinfo.value)
    assert "oos_sharpe" in message
    assert "oos_round_trips" in message


def test_a_metric_of_none_is_refused_rather_than_stored(store):
    with pytest.raises(ValidationReportStoreError) as excinfo:
        store.save(_Report(deflated_sharpe=None))  # type: ignore[arg-type]
    assert "deflated_sharpe" in str(excinfo.value)
    assert store.count() == 0


# ------------------------------------------------------------------ the verdict


def test_a_row_does_not_hand_its_verdict_to_a_rebuilt_report(store):
    """A rehydrated report recomputes ``passed`` from the metrics.

    The verdict columns are stored because the data model requires the verdict
    to be persisted, and they are excluded from what a report is rebuilt from,
    so ``passed = 1`` on a row whose ``oos_sharpe`` is 0.5 cannot launder a
    promotion through the read path.
    """
    store.save(_passing())
    _tamper(store, passed=1, oos_sharpe=0.5)

    row = store.stored_row("sma_cross", "0f1e2d3c4b5a6978")
    assert row is not None
    kwargs = row.as_report_kwargs()
    assert "passed" not in kwargs
    assert "failures" not in kwargs
    assert "survives_2x_costs" not in kwargs
    assert kwargs["oos_sharpe"] == pytest.approx(0.5)
    # What a report is rebuilt from: identity plus the metrics, and nothing else.
    assert set(kwargs) == set(ValidationReportRow.METRIC_FIELDS)
    assert set(kwargs) == set(REPORT_FIELDS)
    assert row.stored_passed is True  # the record still says what it said
    # ...and what the store hands back says the opposite.
    assert store.get("sma_cross", "0f1e2d3c4b5a6978").passed is False


def test_a_tampered_row_cannot_launder_a_promotion(store):
    """The end-to-end control: a row edited to pass reads as a failure.

    ``promotion_status`` is the answer an operator's screen shows, so it is the
    one that has to be right. It is driven here by a row whose verdict column
    says ``passed = 1`` while its metrics say otherwise, which is what a file
    edit or a buggy writer looks like.
    """
    store.save(_passing())
    assert store.promotion_status("sma_cross") == "passed"

    _tamper(store, oos_sharpe=0.1, oos_round_trips=3)
    assert store.promotion_status("sma_cross") == "not_promoted"
    assert store.is_promotable("sma_cross") is False

    # ...and the report itself names every criterion that now fails, rather than
    # reproducing the stored verdict it was supposed to replace.
    report = store.latest("sma_cross")
    assert report.passed is False
    assert set(report.failed_fields) >= {"oos_sharpe", "oos_round_trips"}


def test_a_row_whose_stored_verdict_disagrees_is_logged(store, caplog):
    """A disagreement is visible, not just ineffective.

    Ineffective is the control; invisible is a bug waiting to be read as a
    number. A row whose stored verdict no longer matches its metrics is either a
    writer bug or a tampered file and both deserve a line in the log.
    """
    store.save(_passing())
    _tamper(store, oos_sharpe=0.1)
    with caplog.at_level(logging.WARNING, logger="tradex_execution.validation_report_store"):
        store.get("sma_cross", "0f1e2d3c4b5a6978")
    assert "recompute to passed=False" in caplog.text


def test_a_failing_report_records_its_failures(store):
    store.save(_passing(net_pnl_at_2x_costs=-5.0, oos_round_trips=4))
    row = store.stored_row("sma_cross", "0f1e2d3c4b5a6978")
    assert row is not None
    assert row.stored_passed is False
    assert row.stored_survives_2x_costs is False
    assert any("net_pnl_at_2x_costs" in f for f in row.stored_failures)
    assert any("oos_round_trips" in f for f in row.stored_failures)


# ------------------------------------------------------- rehydration is the only way


def test_the_store_cannot_be_constructed_without_a_rehydrator(tmp_path):
    """No configuration in which the recompute is optional.

    The promotion thresholds live in ``tradex_analytics`` and ``execution`` may
    not import it, so the store cannot compute a verdict itself. A default
    rehydrator that trusted the stored column would be the laundering this
    refuses, so there is no default: the argument is required, and omitting it
    raises.
    """
    with pytest.raises(TypeError):
        ValidationReportStore(tmp_path / "state.db")  # type: ignore[call-arg]
    with pytest.raises(ValidationReportStoreError):
        ValidationReportStore(tmp_path / "state.db", rehydrate=None)  # type: ignore[arg-type]


def test_nothing_the_store_hands_back_is_a_bare_row(store):
    """``get`` / ``latest`` / ``all_for`` return a report, and a row is reachable
    only under a name that says it is the stored one."""
    store.save(_passing())
    store.save(_passing(report_id="report-2", parameter_set_hash="aaaa0000bbbb1111"))

    for report in (
        store.get("sma_cross", "0f1e2d3c4b5a6978"),
        store.latest("sma_cross"),
        *store.all_for("sma_cross"),
    ):
        assert isinstance(report, TradeValidationReport)
        assert isinstance(report.passed, bool)
        assert not isinstance(report, ValidationReportRow)

    # The raw row is still there for an audit trail, and is a row.
    assert isinstance(
        store.stored_row("sma_cross", "0f1e2d3c4b5a6978"), ValidationReportRow
    )
    assert all(
        isinstance(row, ValidationReportRow) for row in store.stored_rows("sma_cross")
    )


def test_the_rehydrator_is_never_handed_a_verdict_field(store):
    """What the recompute is fed is the metrics and nothing else.

    Checked by calling the rehydrator's own contract: a ``TradeValidationReport``
    built from ``REPORT_FIELDS`` alone is a legal report, so if the store ever
    passed a verdict the construction would raise ``TypeError`` rather than
    quietly accept it. Here it is asserted directly, because the store passing
    ``passed`` is the one change that would make the tamper test above vacuous.
    """
    seen: list[dict[str, Any]] = []

    def _spy(kwargs: dict[str, Any]) -> TradeValidationReport:
        seen.append(dict(kwargs))
        return TradeValidationReport(**kwargs)

    store._rehydrate = _spy  # type: ignore[attr-defined]  # noqa: SLF001 — same package
    store.save(_passing())
    store.get("sma_cross", "0f1e2d3c4b5a6978")
    store.latest("sma_cross")
    store.all_for("sma_cross")

    assert seen, "the rehydrator was never called"
    for kwargs in seen:
        assert set(kwargs) == set(REPORT_FIELDS)
        assert not {"passed", "failures", "survives_2x_costs"} & set(kwargs)
    assert len(PROMOTION_CRITERIA) == 6


# ---------------------------------------------------------------- promotion status


def test_a_strategy_with_no_report_is_not_evaluated(store):
    assert store.promotion_status("never_backtested") == "not_evaluated"
    assert store.is_promotable("never_backtested") is False
    assert store.get("never_backtested", "0f1e2d3c4b5a6978") is None


def test_promotion_status_reflects_the_recomputed_verdict(store):
    store.save(_passing())
    assert store.promotion_status("sma_cross") == "passed"
    assert store.is_promotable("sma_cross") is True

    store.save(_passing(report_id="report-2", parameter_set_hash="aaaa0000bbbb1111",
                        oos_sharpe=0.2))
    # The newest parameter set is the one that answers for the strategy.
    report = store.latest("sma_cross")
    assert report is not None
    assert report.parameter_set_hash == "aaaa0000bbbb1111"
    assert store.promotion_status("sma_cross") == "not_promoted"
    assert store.is_promotable("sma_cross") is False


def test_not_evaluated_is_not_promotable_even_though_a_passed_row_exists(store):
    store.save(_passing())
    assert store.promotion_status("bollinger_breakout") == "not_evaluated"
    assert store.is_promotable("bollinger_breakout") is False


# -------------------------------------------------------------- same file as the log


def test_the_report_shares_the_event_store_database_file(tmp_path):
    """One SQLite file, two tables.

    The event store owns the file; this store opens the same path and adds its
    own table, so a single ``persistence.path`` still holds the whole audit
    trail.  Neither constructor may need the other.
    """
    from tradex_domain.events import ErrorOccurred
    from tradex_execution.sqlite_event_store import SQLiteEventStore

    path = tmp_path / "state.db"
    events = SQLiteEventStore(path)
    reports = ValidationReportStore(path, rehydrate=_rehydrate)
    try:
        events.append(ErrorOccurred(error=ValueError("boom")))
        reports.save(_passing())
    finally:
        events.close()
        reports.close()

    events2 = SQLiteEventStore(path)
    reports2 = ValidationReportStore(path, rehydrate=_rehydrate)
    try:
        # ``ErrorOccurred`` is not an order event, so it lands in the "system"
        # stream; counting every stream is what proves the row is still there.
        assert events2.count("*") == 1, "the event log survived the report write"
        assert reports2.get("sma_cross", "0f1e2d3c4b5a6978") is not None
        tables = {
            row[0]
            for row in reports2.connection().execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"events", "tradex_validation_report"} <= tables
    finally:
        events2.close()
        reports2.close()


def test_the_table_is_not_recreated_with_a_different_shape(tmp_path):
    """Opening an existing database twice is the normal case, not a special
    one, so the DDL has to be idempotent."""
    path = tmp_path / "state.db"
    first = ValidationReportStore(path, rehydrate=_rehydrate)
    first.save(_passing())
    first.close()
    second = ValidationReportStore(path, rehydrate=_rehydrate)
    try:
        assert second.count() == 1
        second.save(_passing(report_id="report-2", parameter_set_hash="aaaa0000bbbb1111"))
        assert second.count() == 2
    finally:
        second.close()
