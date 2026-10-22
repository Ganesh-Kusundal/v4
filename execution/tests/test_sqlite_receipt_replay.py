"""An idempotency replay must return the same typed receipt as the first call."""

from __future__ import annotations

import json
import sqlite3

from tradex_domain.enums import OrderStatus
from tradex_domain.execution import OrderReceipt
from tradex_domain.value_objects import CorrelationId, OrderId
from tradex_execution.sqlite_store import SQLiteIdempotencyGuard


def test_receipt_round_trips_through_the_guard(tmp_path) -> None:
    db = tmp_path / "guard.db"
    key = CorrelationId(value="receipt-1")
    receipt = OrderReceipt(
        order_id=OrderId(value="o-1"),
        status=OrderStatus.ACK,
        message="submitted",
    )

    first = SQLiteIdempotencyGuard(db)
    try:
        assert first.check_and_reserve(key) is None
        first.record_result(key, receipt)
    finally:
        first.close()

    reopened = SQLiteIdempotencyGuard(db)
    try:
        duplicate = reopened.check_and_reserve(key)
        assert isinstance(duplicate.result, OrderReceipt)
        assert duplicate.result == receipt
    finally:
        reopened.close()


def test_legacy_order_id_marker_decodes_to_a_receipt(tmp_path) -> None:
    """A row written by the previous release still replays, as a receipt."""
    db = tmp_path / "guard.db"
    guard = SQLiteIdempotencyGuard(db)
    try:
        with sqlite3.connect(str(db)) as other:
            other.execute(
                "INSERT INTO idempotency (correlation_id, status, result) "
                "VALUES (?, 'completed', ?)",
                ("legacy-1", json.dumps({"__tradex_order_id__": "old-7"})),
            )

        duplicate = guard.check_and_reserve(CorrelationId(value="legacy-1"))
        assert isinstance(duplicate.result, OrderReceipt)
        assert duplicate.result.order_id == OrderId(value="old-7")
        assert duplicate.result.status is OrderStatus.SUBMITTED
        assert duplicate.result.message == "idempotency_replay"
    finally:
        guard.close()
