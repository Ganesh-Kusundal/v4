"""Durable storage for the trade validation report.

The report is the artefact the promotion decision is read from, so it outlives
the process that computed it.  The table lives in the **same SQLite file as the
event store** — pass the same ``persistence.path`` — and holds its own table
beside ``events``, so one file is the whole audit trail and neither store has to
know the other exists.

Schema
------
``tradex_validation_report`` keyed ``(strategy_id, parameter_set_hash)``, one row
per strategy per parameter set: re-running the same parameters replaces the
report rather than accumulating a history nobody reads, and the history that
matters is in the event log.  Columns are the report's fields, bound **by name**,
and ``oos_round_trips`` is declared before ``oos_fills`` so that even a
positional bind could not swap the two.

Why those two columns are separate ``NOT NULL`` columns
-------------------------------------------------------
``BacktestResult.num_trades`` counts filled orders, the chart route publishes it
under the key ``num_trades``, and the interface labels it "Trades" — a live
mislabelling bug where 55 fills were read as 55 trades on a run with 27 round
trips.  A single column for both would reintroduce it at the storage layer, and a
report that stored a fill count where the promotion bar reads a round trip count
would pass a strategy that never made a hundred trades.

The verdict columns are stored because the data model requires the verdict to be
persisted — and they are persisted for a reader, not for a decision.  **The only
way to obtain a report from this store is to rehydrate one**, and rehydration
recomputes the verdict from the metrics (``TradeValidationReport.__post_init__``):
:meth:`ValidationReportStore.get`, :meth:`~ValidationReportStore.latest` and
:meth:`~ValidationReportStore.all_for` all return the rehydrated report, and
:meth:`~ValidationReportStore.promotion_status` reads *that*, so a row UPDATEd to
``passed = 1`` on metrics that fail the bar reads as a failure.

**The rehydrator is a constructor argument and it is required.**  The promotion
thresholds live in ``tradex_analytics``, and the dependency direction allows
``execution -> tradex_domain`` alone, so this package cannot compute a verdict —
importing ``analytics`` here to "just recompute it" would invert the graph the
boundary test enforces.  Rather than take a trust-the-column shortcut around
that, the store is constructed with the callable that *does* build a report and
has no constructor at all without one: there is no configuration of this class
in which a caller can read a stored verdict as an answer, because there is no
configuration in which the store can hand back a report that was not recomputed.
The raw row is still reachable, as :meth:`~ValidationReportStore.stored_row` and
:meth:`~ValidationReportStore.stored_rows`, for the audit trail and for
comparing what was written against what recomputes — and a disagreement between
the two is logged at WARNING, because a row whose stored verdict no longer
matches its metrics is either a bug in the writer or a tampered file and both
are worth a line in the log.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

_log = logging.getLogger(__name__)

#: The report's fields, in the order they are declared.  ``oos_round_trips``
#: precedes ``oos_fills`` deliberately (see the module docstring).
REPORT_COLUMNS: tuple[str, ...] = (
    "report_id",
    "strategy_id",
    "parameter_set_hash",
    "oos_sharpe",
    "oos_round_trips",
    "oos_fills",
    "oos_span_days",
    "expectancy",
    "profit_factor",
    "max_drawdown_pct",
    "deflated_sharpe",
    "param_stability_share_of_grid",
    "net_pnl_at_2x_costs",
    "bar_frequency",
    "survives_2x_costs",
    "passed",
    "failures",
)

#: What a caller hands :meth:`ValidationReportStore.save`.  Checked at runtime by
#: :meth:`ValidationReportStore._read_fields`, which names every field that is
#: missing, so the contract holds without this package importing
#: ``tradex_analytics.validation_report`` — the dependency direction that keeps
#: the store a place to put rows rather than a place that knows what a report is.
_METRIC_ATTRIBUTES: tuple[str, ...] = (
    "report_id",
    "strategy_id",
    "parameter_set_hash",
    "oos_sharpe",
    "oos_round_trips",
    "oos_fills",
    "oos_span_days",
    "expectancy",
    "profit_factor",
    "max_drawdown_pct",
    "deflated_sharpe",
    "param_stability_share_of_grid",
    "net_pnl_at_2x_costs",
    "bar_frequency",
)

__all__ = [
    "REPORT_COLUMNS",
    "ValidationReportRow",
    "ValidationReportStore",
    "ValidationReportStoreError",
]


class ValidationReportStoreError(RuntimeError):
    """A report could not be persisted or read.

    Loud on purpose.  A validation report that is written with a field missing,
    or read back as a partial row, is a promotion decision resting on a number
    that was never produced.
    """


@dataclass(frozen=True, slots=True)
class ValidationReportRow:
    """One stored report, as it sits in the table.

    **A row is not a report.**  It is the evidence a report is rebuilt from, and
    it is deliberately the wrong shape for a promotion decision: the verdict is
    exposed as ``stored_*`` and never as ``passed``, and
    :meth:`as_report_kwargs` — the only thing a report is constructed from —
    carries no verdict field at all.  A caller that wants a verdict calls
    :meth:`ValidationReportStore.get`, which rehydrates and recomputes.
    """

    report_id: str
    strategy_id: str
    parameter_set_hash: str
    oos_sharpe: float
    oos_round_trips: int
    oos_fills: int
    oos_span_days: float
    expectancy: float
    profit_factor: float
    max_drawdown_pct: float
    deflated_sharpe: float
    param_stability_share_of_grid: float
    net_pnl_at_2x_costs: float
    bar_frequency: str
    stored_survives_2x_costs: bool
    stored_passed: bool
    stored_failures: tuple[str, ...]
    created_at: str

    #: What a report is rebuilt from: identity and metrics, never the verdict.
    METRIC_FIELDS: ClassVar[tuple[str, ...]] = _METRIC_ATTRIBUTES

    def as_report_kwargs(self) -> dict[str, Any]:
        """Constructor arguments for a report rebuilt from this row.

        The verdict is recomputed, so this mapping carries no ``passed``,
        ``failures`` or ``survives_2x_costs`` — which is the whole reason a
        tampered row cannot promote a strategy.
        """
        return {name: getattr(self, name) for name in self.METRIC_FIELDS}


class ValidationReportStore:
    """The ``tradex_validation_report`` table, beside the event log.

    Parameters
    ----------
    db_path:
        The SQLite file.  Pass the same path as ``SQLiteEventStore`` — this
        store adds its own table to that file and never reads ``events``.
        ``":memory:"`` gives an ephemeral store for tests.
    rehydrate:
        **Required.**  Builds a report from a row's identity and metrics — the
        identity and metrics only, never the verdict, which is what makes the
        recompute mandatory rather than optional.  It is injected because the
        promotion bar lives in ``tradex_analytics`` and this package may not
        import it (see the module docstring), so the composition root is where
        the two are joined::

            ValidationReportStore(
                persistence.path,
                rehydrate=lambda kwargs: TradeValidationReport(**kwargs),
            )

        Its result is returned as-is, so the type of a report from this store is
        whatever the rehydrator builds.  The store reads ``passed`` and
        ``failures`` off it for :meth:`promotion_status` and logs
        ``survives_2x_costs`` divergence; it never writes them back.
    """

    _DDL = """
    CREATE TABLE IF NOT EXISTS tradex_validation_report (
        report_id                     TEXT    NOT NULL,
        strategy_id                   TEXT    NOT NULL,
        parameter_set_hash            TEXT    NOT NULL,
        oos_sharpe                    REAL    NOT NULL,
        oos_round_trips               INTEGER NOT NULL,
        oos_fills                     INTEGER NOT NULL,
        oos_span_days                 REAL    NOT NULL,
        expectancy                    REAL    NOT NULL,
        profit_factor                 REAL    NOT NULL,
        max_drawdown_pct              REAL    NOT NULL,
        deflated_sharpe               REAL    NOT NULL,
        param_stability_share_of_grid REAL    NOT NULL,
        net_pnl_at_2x_costs           REAL    NOT NULL,
        bar_frequency                 TEXT    NOT NULL,
        survives_2x_costs             INTEGER NOT NULL,
        passed                        INTEGER NOT NULL,
        failures                      TEXT    NOT NULL,
        created_at                    TEXT    NOT NULL,
        PRIMARY KEY (report_id),
        UNIQUE (strategy_id, parameter_set_hash)
    );
    CREATE INDEX IF NOT EXISTS tradex_validation_report_strategy
        ON tradex_validation_report (strategy_id, created_at);
    """

    def __init__(
        self,
        db_path: str | Path = ":memory:",
        *,
        rehydrate: Callable[[dict[str, Any]], Any],
    ) -> None:
        if not callable(rehydrate):
            raise ValidationReportStoreError(
                "rehydrate must be a callable that builds a report from a row's "
                "identity and metrics. It is required because this package may "
                "not import tradex_analytics, where the promotion thresholds "
                "live: without it the store could only answer a promotion "
                "question by trusting the verdict column it was told to persist, "
                "which is exactly the laundering the recompute prevents."
            )
        self._path = str(db_path)
        self._rehydrate = rehydrate
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        # Rows by name: a column added later cannot shift a metric sideways.
        self._conn.row_factory = sqlite3.Row
        if db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(self._DDL)
        self._conn.commit()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ writes

    def save(self, report: Any) -> str:
        """Persist *report*, replacing any report for the same parameters.

        The columns are bound by name, so ``oos_fills`` cannot land in
        ``oos_round_trips`` however the tuple is ordered.
        """
        values = self._read_fields(report)
        columns = (*REPORT_COLUMNS, "created_at")
        payload = (
            *(_as_payload(name, values[name]) for name in REPORT_COLUMNS),
            datetime.now(UTC).isoformat(),
        )
        placeholders = ", ".join("?" for _ in columns)
        with self._lock:
            self._conn.execute(
                f"INSERT OR REPLACE INTO tradex_validation_report "  # noqa: S608 - fixed column list
                f"({', '.join(columns)}) VALUES ({placeholders})",
                payload,
            )
            self._conn.commit()
        return str(values["report_id"])

    @staticmethod
    def _read_fields(report: Any) -> dict[str, Any]:
        """Every field the row needs, or a failure naming all of the missing."""
        values: dict[str, Any] = {}
        missing: list[str] = []
        for name in (*_METRIC_ATTRIBUTES, "passed", "survives_2x_costs", "failures"):
            value = getattr(report, name, None)
            if value is None:
                missing.append(name)
            else:
                values[name] = value
        if missing:
            raise ValidationReportStoreError(
                f"this report cannot be persisted: {', '.join(missing)} "
                "carries no value. Every field the row holds is a field the "
                "promotion bar reads, so a report missing one is not a report."
            )
        return values

    # ------------------------------------------------------------------- reads

    def _report(self, row: sqlite3.Row) -> Any:
        """Rehydrate *row* into a report, and say so if the row disagrees.

        The only path from a row to a report, so the only path on which a
        verdict can be read.  A row's stored ``passed`` / ``survives_2x_costs``
        are compared against the recomputed ones purely to make tampering and
        writer bugs *visible*: the recomputed values are what is returned, and
        the stored ones are never used as an answer.
        """
        stored = _to_row(row)
        report = self._rehydrate(stored.as_report_kwargs())
        if bool(getattr(report, "passed", False)) != stored.stored_passed:
            _log.warning(
                "validation report %s (%s/%s) stores passed=%s but its metrics "
                "recompute to passed=%s. The recomputed verdict is the one being "
                "returned and the one promotion_status reads; a disagreement "
                "here means the row was written by something other than this "
                "store's save(), or the file was edited.",
                stored.report_id, stored.strategy_id, stored.parameter_set_hash,
                stored.stored_passed, bool(getattr(report, "passed", False)),
            )
        if bool(getattr(report, "survives_2x_costs", False)) != stored.stored_survives_2x_costs:
            _log.warning(
                "validation report %s stores survives_2x_costs=%s but its "
                "metrics recompute to %s.",
                stored.report_id, stored.stored_survives_2x_costs,
                bool(getattr(report, "survives_2x_costs", False)),
            )
        return report

    def get(self, strategy_id: str, parameter_set_hash: str) -> Any | None:
        """The report for one strategy and parameter set, or ``None``.

        A **rehydrated report**, so its verdict was computed from its metrics
        when it was read.  The row it came from is available through
        :meth:`stored_row` for anyone auditing what was written.
        """
        row = self._conn.execute(
            "SELECT * FROM tradex_validation_report "
            "WHERE strategy_id = ? AND parameter_set_hash = ?",
            (strategy_id, parameter_set_hash),
        ).fetchone()
        return self._report(row) if row else None

    def latest(self, strategy_id: str) -> Any | None:
        """The most recently written report for a strategy, or ``None``.

        A strategy is judged on the parameter set it was last evaluated with, so
        this is what a promotion question resolves to.  Rehydrated, like
        :meth:`get`.
        """
        row = self._conn.execute(
            "SELECT * FROM tradex_validation_report WHERE strategy_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (strategy_id,),
        ).fetchone()
        return self._report(row) if row else None

    def all_for(self, strategy_id: str) -> list[Any]:
        """Every stored report for a strategy, newest first. Rehydrated."""
        rows = self._conn.execute(
            "SELECT * FROM tradex_validation_report WHERE strategy_id = ? "
            "ORDER BY created_at DESC, rowid DESC",
            (strategy_id,),
        ).fetchall()
        return [self._report(row) for row in rows]

    def stored_row(
        self, strategy_id: str, parameter_set_hash: str
    ) -> ValidationReportRow | None:
        """The raw row, verdict column and all, for the audit trail.

        Named ``stored_*`` on purpose: this is what the file says, not what the
        promotion bar concludes, and nothing on the decision path reads it.  Use
        :meth:`get` for a verdict.
        """
        row = self._conn.execute(
            "SELECT * FROM tradex_validation_report "
            "WHERE strategy_id = ? AND parameter_set_hash = ?",
            (strategy_id, parameter_set_hash),
        ).fetchone()
        return _to_row(row) if row else None

    def stored_rows(self, strategy_id: str) -> list[ValidationReportRow]:
        """Every raw row for a strategy, newest first. See :meth:`stored_row`."""
        rows = self._conn.execute(
            "SELECT * FROM tradex_validation_report WHERE strategy_id = ? "
            "ORDER BY created_at DESC, rowid DESC",
            (strategy_id,),
        ).fetchall()
        return [_to_row(row) for row in rows]

    def count(self) -> int:
        """How many reports are stored."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM tradex_validation_report"
        ).fetchone()
        return int(row[0]) if row else 0

    def promotion_status(self, strategy_id: str) -> str:
        """``passed``, ``not_promoted`` or ``not_evaluated``.

        Reads the **rehydrated** report, so the answer is the promotion bar
        applied to the stored metrics rather than the verdict column someone
        wrote.  ``not_evaluated`` is a first class answer, not a missing one: a
        strategy nobody has run has no report, and the safety surface shows it
        as not promotable rather than as a gap.
        """
        report = self.latest(strategy_id)
        if report is None:
            return "not_evaluated"
        return "passed" if bool(getattr(report, "passed", False)) else "not_promoted"

    def is_promotable(self, strategy_id: str) -> bool:
        """Whether this store holds a passing report for *strategy_id*."""
        return self.promotion_status(strategy_id) == "passed"

    def connection(self) -> sqlite3.Connection:
        """The underlying connection — diagnostics and integrity checks only."""
        return self._conn

    def close(self) -> None:
        """Close the connection."""
        self._conn.close()


def _as_payload(name: str, value: Any) -> Any:
    """Encode one field for SQLite."""
    if name == "failures":
        return json.dumps(list(value))
    if name in {"passed", "survives_2x_costs"}:
        return 1 if value else 0
    if name in {"oos_round_trips", "oos_fills"}:
        return int(value)
    return value


def _to_row(row: sqlite3.Row) -> ValidationReportRow:
    """One raw row as a :class:`ValidationReportRow`.

    Read by column name (``sqlite3.Row``), so adding a column cannot silently
    shift a metric into the wrong field.
    """
    record = dict(row)
    return ValidationReportRow(
        report_id=str(record["report_id"]),
        strategy_id=str(record["strategy_id"]),
        parameter_set_hash=str(record["parameter_set_hash"]),
        oos_sharpe=float(record["oos_sharpe"]),
        oos_round_trips=int(record["oos_round_trips"]),
        oos_fills=int(record["oos_fills"]),
        oos_span_days=float(record["oos_span_days"]),
        expectancy=float(record["expectancy"]),
        profit_factor=float(record["profit_factor"]),
        max_drawdown_pct=float(record["max_drawdown_pct"]),
        deflated_sharpe=float(record["deflated_sharpe"]),
        param_stability_share_of_grid=float(record["param_stability_share_of_grid"]),
        net_pnl_at_2x_costs=float(record["net_pnl_at_2x_costs"]),
        bar_frequency=str(record["bar_frequency"]),
        stored_survives_2x_costs=bool(record["survives_2x_costs"]),
        stored_passed=bool(record["passed"]),
        stored_failures=tuple(json.loads(record["failures"])),
        created_at=str(record["created_at"]),
    )
