"""Durable per-strategy risk baselines — capital and drawdown across restarts.

The daily-loss and drawdown guards in :class:`tradex_execution.risk.RiskManager`
compare today's P&L against a day-start baseline and a running peak, both of
which live in memory. A crash-restart resets them, so an operator who loses
₹50k against a ₹30k limit at 11:00 and restarts has a fresh zero baseline and a
drawdown breaker that no longer fires (AC-9). This store persists the baseline
and the peak per strategy so a restart reads them back instead of recomputing a
lossy zero.

It reuses the **existing** SQLite path — the same database file as
``SQLiteOrderStore`` (``AppConfig.persistence.path``) — so there is no new
subsystem and no new config knob. The table is created lazily and independently
so a database that only holds orders still opens cleanly, and two stores on the
same file coexist (each owns its own table).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from tradex_execution._sqlite import configure_sqlite

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StrategyBaseline:
    """One strategy's day-start P&L and running peak for a single date.

    ``session_date`` is the calendar date the baseline was taken on. A stored
    baseline from a previous date is not reused — the day rolls forward, exactly
    as the in-memory path does — so a stale-date row reads back as "no baseline
    for today" rather than as yesterday's loss.
    """

    strategy_id: str
    session_date: str
    base_pnl: Decimal
    peak_pnl: Decimal


class SQLiteRiskBaselineStore:
    """Per-strategy capital/drawdown baselines, persisted in SQLite.

    One row per ``strategy_id``. The primary key is the strategy, so a restart
    reads back the last known baseline and peak. Money is stored as TEXT
    (Decimal ``str``) to match the convention ``SQLiteOrderStore`` uses for
    prices and quantities — never a float.
    """

    _CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS risk_baselines (
        strategy_id TEXT PRIMARY KEY,
        session_date TEXT NOT NULL,
        base_pnl TEXT NOT NULL,
        peak_pnl TEXT NOT NULL
    )
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        # check_same_thread=False: written from the risk check thread on the
        # execution path, matching the peer stores which share one connection.
        self._conn = sqlite3.connect(
            str(db_path) if db_path != ":memory:" else ":memory:",
            check_same_thread=False,
        )
        self._write_lock = threading.Lock()
        configure_sqlite(self._conn)
        self._conn.execute(self._CREATE_TABLE)
        self._conn.commit()

    @staticmethod
    def _to_decimal(text: str) -> Decimal:
        """Parse a stored money string, treating corruption as zero.

        A row that cannot be read must not be allowed to crash the risk check
        path; the worst case of reading it as zero is that a restart treats the
        strategy as flat, which the fresh-mark guard still covers. This never
        *raises* into the money path.
        """
        try:
            return Decimal(text)
        except (InvalidOperation, TypeError, ValueError):
            log.warning("Unreadable risk baseline %r; treating as 0", text)
            return Decimal(0)

    def load(self, strategy_id: str) -> StrategyBaseline | None:
        """Return the stored baseline for *strategy_id*, or ``None``."""
        row = self._conn.execute(
            "SELECT strategy_id, session_date, base_pnl, peak_pnl "
            "FROM risk_baselines WHERE strategy_id = ?",
            (strategy_id,),
        ).fetchone()
        if row is None:
            return None
        return StrategyBaseline(
            strategy_id=row[0],
            session_date=row[1],
            base_pnl=self._to_decimal(row[2]),
            peak_pnl=self._to_decimal(row[3]),
        )

    def load_all(self) -> dict[str, StrategyBaseline]:
        """Return every stored baseline keyed by strategy id."""
        rows = self._conn.execute(
            "SELECT strategy_id, session_date, base_pnl, peak_pnl FROM risk_baselines"
        ).fetchall()
        return {
            row[0]: StrategyBaseline(
                strategy_id=row[0],
                session_date=row[1],
                base_pnl=self._to_decimal(row[2]),
                peak_pnl=self._to_decimal(row[3]),
            )
            for row in rows
        }

    def save(self, baseline: StrategyBaseline) -> None:
        """Insert or replace the baseline for one strategy."""
        with self._write_lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO risk_baselines
                   (strategy_id, session_date, base_pnl, peak_pnl)
                   VALUES (?, ?, ?, ?)""",
                (
                    baseline.strategy_id,
                    baseline.session_date,
                    str(baseline.base_pnl),
                    str(baseline.peak_pnl),
                ),
            )
            self._conn.commit()

    def clear(self, strategy_id: str) -> None:
        """Drop the baseline for one strategy (a new session date rolls it)."""
        with self._write_lock:
            self._conn.execute(
                "DELETE FROM risk_baselines WHERE strategy_id = ?", (strategy_id,)
            )
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()


__all__ = ["SQLiteRiskBaselineStore", "StrategyBaseline"]
