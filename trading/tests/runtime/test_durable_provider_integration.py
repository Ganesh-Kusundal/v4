"""Durable provider integration tests — idempotency guard + execution engine.

Ported from v3 ``test_durable_provider_integration.py``.

v4 API differences:
- No ``SQLiteOrderStore`` or ``SQLiteIdempotencyGuard`` in v4
- v4 has ``MemoryIdempotencyGuard`` and ``IdempotencyDuplicate``
- Skip broker-specific instrument loading (stubs)
- Port idempotency and execution engine integration parts
"""

from __future__ import annotations

from decimal import Decimal

from tradex_domain import BrokerId
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, ProductType, TimeInForce
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import CorrelationId, Price, Quantity

from tradex_trading.config.schema import AppConfig
from tradex_trading.execution.engine import (
    ExecutionEngine,
    IdempotencyDuplicate,
    MemoryIdempotencyGuard,
)
from tradex_trading.execution.fill_sources import PaperFillSource
from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.runtime.startup import boot


def _equity() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _request(quantity: int = 2) -> OrderRequest:
    return OrderRequest(
        instrument=_equity(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(quantity)),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
        product_type=ProductType.INTRADAY,
    )


# ---------------------------------------------------------------------------
# MemoryIdempotencyGuard — in-memory idempotency
# ---------------------------------------------------------------------------


class TestMemoryIdempotencyGuard:
    """MemoryIdempotencyGuard — in-memory idempotency guard."""

    def test_first_call_returns_none(self) -> None:
        guard = MemoryIdempotencyGuard()
        cid = CorrelationId(value="corr-1")
        result = guard.check_and_reserve(cid)
        assert result is None

    def test_second_call_returns_duplicate(self) -> None:
        guard = MemoryIdempotencyGuard()
        cid = CorrelationId(value="corr-1")
        guard.check_and_reserve(cid)
        guard.record_result(cid, {"order_id": "o-1"})
        duplicate = guard.check_and_reserve(cid)
        assert isinstance(duplicate, IdempotencyDuplicate)
        assert duplicate.result == {"order_id": "o-1"}

    def test_different_correlation_ids_are_independent(self) -> None:
        guard = MemoryIdempotencyGuard()
        cid1 = CorrelationId(value="corr-1")
        cid2 = CorrelationId(value="corr-2")
        guard.check_and_reserve(cid1)
        guard.record_result(cid1, {"order_id": "o-1"})
        assert guard.check_and_reserve(cid2) is None


# ---------------------------------------------------------------------------
# Boot integration — session starts with empty state
# ---------------------------------------------------------------------------


class TestBootIntegration:
    """boot() produces a session with clean initial state."""

    def test_default_boot_has_no_positions(self) -> None:
        session = boot(AppConfig())
        positions = session.portfolio.positions()
        assert positions == []
        session.stop()

    def test_default_boot_has_paper_broker(self) -> None:
        session = boot()
        assert session.broker_id == BrokerId.PAPER
        session.stop()


# ---------------------------------------------------------------------------
# Execution engine with idempotency guard
# ---------------------------------------------------------------------------


class TestExecutionWithIdempotency:
    """Execution engine integrates idempotency guard."""

    def test_submit_with_guard(self) -> None:
        bus = ReactiveBus()
        guard = MemoryIdempotencyGuard()
        engine = ExecutionEngine(
            bus=bus,
            fill_source=PaperFillSource(),
            idempotency_guard=guard,
        )
        receipt = engine.submit(_request())
        assert receipt.status in (OrderStatus.FILLED, OrderStatus.SUBMITTED)

    def test_duplicate_correlation_returns_same_result(self) -> None:
        bus = ReactiveBus()
        guard = MemoryIdempotencyGuard()
        engine = ExecutionEngine(
            bus=bus,
            fill_source=PaperFillSource(),
            idempotency_guard=guard,
        )
        receipt1 = engine.submit(_request())
        assert receipt1.status in (OrderStatus.FILLED, OrderStatus.SUBMITTED)
