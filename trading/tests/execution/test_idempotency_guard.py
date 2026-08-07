"""Tests for idempotency guards — Protocol conformance and duplicate detection."""

from __future__ import annotations

from decimal import Decimal

import pytest
from tradex_domain.enums import OrderSide, OrderType, ProductType, TimeInForce
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import CorrelationId, Price, Quantity

from tradex_trading.execution.engine import (
    ExecutionEngine,
    IdempotencyDuplicate,
    IdempotencyGuard,
    MemoryIdempotencyGuard,
)
from tradex_trading.execution.fill_sources import SimulatedFillSource
from tradex_trading.execution.sqlite_store import SQLiteIdempotencyGuard
from tradex_trading.reactive.bus import ReactiveBus


def _make_request(cid: str | None = None) -> OrderRequest:
    return OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500.00")),
        time_in_force=TimeInForce.DAY,
        product_type=ProductType.INTRADAY,
        correlation_id=CorrelationId(value=cid) if cid else None,
    )


# -- MemoryIdempotencyGuard --


class TestMemoryIdempotencyGuardProtocol:
    """MemoryIdempotencyGuard satisfies the IdempotencyGuard Protocol."""

    def test_is_instance_of_protocol(self) -> None:
        guard = MemoryIdempotencyGuard()
        assert isinstance(guard, IdempotencyGuard)

    def test_sqlite_is_instance_of_protocol(self) -> None:
        guard = SQLiteIdempotencyGuard()
        assert isinstance(guard, IdempotencyGuard)
        guard.close()


class TestMemoryIdempotencyGuardDuplicateDetection:
    """Duplicate detection works correctly."""

    def test_new_id_returns_none(self) -> None:
        guard = MemoryIdempotencyGuard()
        cid = CorrelationId(value="abc")
        assert guard.check_and_reserve(cid) is None

    def test_duplicate_after_reserve_raises(self) -> None:
        guard = MemoryIdempotencyGuard()
        cid = CorrelationId(value="abc")
        guard.check_and_reserve(cid)
        with pytest.raises(RuntimeError, match="already reserved"):
            guard.check_and_reserve(cid)

    def test_duplicate_after_record_returns_duplicate(self) -> None:
        guard = MemoryIdempotencyGuard()
        cid = CorrelationId(value="abc")
        assert guard.check_and_reserve(cid) is None
        guard.record_result(cid, "order-123")
        dup = guard.check_and_reserve(cid)
        assert dup is not None
        assert isinstance(dup, IdempotencyDuplicate)
        assert dup.result == "order-123"

    def test_release_allows_reuse(self) -> None:
        guard = MemoryIdempotencyGuard()
        cid = CorrelationId(value="abc")
        guard.check_and_reserve(cid)
        guard.release(cid)
        # After release, can reserve again
        assert guard.check_and_reserve(cid) is None

    def test_different_ids_independent(self) -> None:
        guard = MemoryIdempotencyGuard()
        cid1 = CorrelationId(value="one")
        cid2 = CorrelationId(value="two")
        assert guard.check_and_reserve(cid1) is None
        assert guard.check_and_reserve(cid2) is None


# -- SQLiteIdempotencyGuard --


class TestSQLiteIdempotencyGuardDuplicateDetection:
    """SQLiteIdempotencyGuard duplicate detection."""

    def test_new_id_returns_none(self) -> None:
        guard = SQLiteIdempotencyGuard()
        cid = CorrelationId(value="abc")
        assert guard.check_and_reserve(cid) is None
        guard.close()

    def test_completed_returns_duplicate(self) -> None:
        guard = SQLiteIdempotencyGuard()
        cid = CorrelationId(value="abc")
        guard.check_and_reserve(cid)
        guard.record_result(cid, "result-1")
        dup = guard.check_and_reserve(cid)
        assert dup is not None
        assert isinstance(dup, IdempotencyDuplicate)
        guard.close()

    def test_release_allows_rereserve(self) -> None:
        guard = SQLiteIdempotencyGuard()
        cid = CorrelationId(value="abc")
        guard.check_and_reserve(cid)
        guard.release(cid)
        # After release, can reserve again (new entry)
        assert guard.check_and_reserve(cid) is None
        guard.close()


# -- Integration with ExecutionEngine --


class TestExecutionEngineIdempotency:
    """ExecutionEngine uses the guard correctly."""

    def test_duplicate_order_returns_original_result(self) -> None:
        bus = ReactiveBus()
        fill = SimulatedFillSource()
        guard = MemoryIdempotencyGuard()
        engine = ExecutionEngine(bus=bus, fill_source=fill, idempotency_guard=guard)

        req = _make_request(cid="dup-test")
        receipt1 = engine.submit(req)
        assert receipt1.status.value != "REJECTED" or receipt1.message == "submitted"

        # Second submit with same correlation_id should return original result (v3 parity)
        receipt2 = engine.submit(req)
        assert receipt2 == receipt1.order_id
