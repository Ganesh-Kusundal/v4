"""Build the ``ValidationReportStore`` the AC-17 safety route reads.

**Why this module exists, and why it is here.** The safety surface answers
"may this strategy trade?" from ``session.validation_report_store``, but
``tradex_runtime`` cannot construct that store: its ``rehydrate`` seam has to
produce a validation report, and the report's class lives in
``tradex_analytics`` — which ``tests/test_import_boundaries.py`` forbids
``tradex_runtime`` from importing. ``tradex_interfaces`` is a layer that can see
both the store (``tradex_execution``) and the promotion bar
(``tradex_analytics``), and it is already the composition root for the HTTP
surface that reads the result. So the join belongs here.

**Why the rehydrator is a parameter and not a lambda.** AC-13 makes
``tradex_replay.validation`` the single writer of a validation report: a second
construction site anywhere in production is a second promotion decision, and
``trading/tests/replay/test_validation_entry_point.py`` fails the build on one.
So this module does not build a report and does not name the report class. It
assembles the store around a rehydrator the composition root supplies, which
keeps the "one writer" property a structural fact rather than a convention.

The store deliberately does not persist a verdict it can trust: it rebuilds the
report from the stored *metrics* on every read and re-applies the promotion
bar. A row whose ``passed`` column was edited in the database therefore still
answers ``not_promotable`` when its metrics miss the bar, which is what makes
the safety surface worth reading.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from tradex_execution.validation_report_store import ValidationReportStore

__all__ = ["build_validation_report_store"]


def build_validation_report_store(
    db_path: str | Path | None,
    rehydrate: Callable[[dict[str, Any]], Any],
) -> ValidationReportStore | None:
    """A :class:`ValidationReportStore` over *db_path*, or ``None``.

    ``None`` is returned — never a store over a throwaway path — when
    *db_path* is falsy. An in-memory store would answer ``not_evaluated`` for
    every strategy forever, and the safety surface would report that as a real
    promotion standing rather than as the absence of any record. Absence is the
    honest answer when no database is configured.

    Parameters
    ----------
    db_path:
        The SQLite file. The same path the event log uses, so a report written
        by a replay run is readable by a live session.
    rehydrate:
        Builds a report from a row's identity and metrics. **Required, and
        injected rather than written here** — see the module docstring: the
        report class may only be constructed by its single owner in
        ``tradex_replay.validation``.
    """
    if not db_path:
        return None
    return ValidationReportStore(db_path, rehydrate=rehydrate)
