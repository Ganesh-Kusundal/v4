"""The idempotency guard must leave no transaction open when it refuses.

A duplicate reserve used to SELECT on the connection that still held the
failed INSERT's implicit transaction, so the connection stayed
``in_transaction``. On a shared file that pins a read snapshot and blocks the
next writer — the "database is locked" failure that only appears on retry.
"""

from __future__ import annotations

import sqlite3

import pytest
from tradex_domain.value_objects import CorrelationId
from tradex_execution.engine import IdempotencyInflight
from tradex_execution.sqlite_store import SQLiteIdempotencyGuard


def _visible_statuses(db_path, key: str) -> list[str]:
    """Read through a SECOND connection: only committed rows are visible."""
    with sqlite3.connect(str(db_path)) as other:
        rows = other.execute(
            "SELECT status FROM idempotency WHERE correlation_id = ?",
            (key,),
        ).fetchall()
    return [row[0] for row in rows]


def test_duplicate_reserve_leaves_no_open_transaction(tmp_path) -> None:
    db = tmp_path / "guard.db"
    key = CorrelationId(value="tx-1")
    guard = SQLiteIdempotencyGuard(db)
    try:
        assert guard.check_and_reserve(key) is None
        guard.record_result(key, "done")

        duplicate = guard.check_and_reserve(key)

        assert duplicate.result == "done"
        assert guard._conn.in_transaction is False  # noqa: SLF001
        assert _visible_statuses(db, "tx-1") == ["completed"]
    finally:
        guard.close()


def test_inflight_reserve_leaves_no_open_transaction(tmp_path) -> None:
    db = tmp_path / "guard.db"
    key = CorrelationId(value="tx-2")
    guard = SQLiteIdempotencyGuard(db)
    try:
        assert guard.check_and_reserve(key) is None

        with pytest.raises(IdempotencyInflight):
            guard.check_and_reserve(key)

        assert guard._conn.in_transaction is False  # noqa: SLF001
        assert _visible_statuses(db, "tx-2") == ["reserved"]
    finally:
        guard.close()


def test_fresh_reservation_is_committed_for_another_connection(tmp_path) -> None:
    db = tmp_path / "guard.db"
    guard = SQLiteIdempotencyGuard(db)
    try:
        assert guard.check_and_reserve(CorrelationId(value="tx-3")) is None
        assert guard._conn.in_transaction is False  # noqa: SLF001
        assert _visible_statuses(db, "tx-3") == ["reserved"]
    finally:
        guard.close()


def test_release_leaves_no_row_and_no_transaction(tmp_path) -> None:
    db = tmp_path / "guard.db"
    key = CorrelationId(value="tx-4")
    guard = SQLiteIdempotencyGuard(db)
    try:
        assert guard.check_and_reserve(key) is None
        guard.release(key)
        assert guard._conn.in_transaction is False  # noqa: SLF001
        assert _visible_statuses(db, "tx-4") == []
    finally:
        guard.close()
