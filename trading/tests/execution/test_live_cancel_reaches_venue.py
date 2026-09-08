"""N1 (P0) — plain live cancels must reach the venue before the OMS flips.

The principal-quant review (architecture-flow-verification-2026-09-04, N1)
found that ``engine.cancel()`` transitions the OMS cache to CANCELLED and
never dispatches the venue for plain (non-bracket) orders — ``_fill.cancel``
→ ``broker.cancel_order`` ran only inside ``trip_kill_switch``. A
"cancelled" live order can therefore still fill at the venue.

These tests pin the fixed contract:

  1. plain order + venue: ``broker.cancel_order`` invoked exactly once,
     BEFORE the OMS cache shows CANCELLED;
  2. venue failure: OMS stays ACK, no ``OrderCancelled`` published, the
     error propagates to the caller (and to the route as a typed 4xx);
  3. brackets still dispatch ``cancel_super_order`` exactly once and never
     the plain ``cancel_order``;
  4. paper/simulated sources (no venue) keep OMS-local semantics:
     CANCELLED, ``OrderCancelled`` published, cid released.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.errors import OrderRejectedError
from tradex_domain.events import OrderCancelled
from tradex_domain.execution import BracketOrderRequest, Order, OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import CorrelationId, OrderId, Price, Quantity

from tradex_trading.execution.engine import (
    ExecutionEngine,
    IdempotencyDuplicate,
    MemoryIdempotencyGuard,
)
from tradex_trading.execution.fill_sources import BrokerFillSource
from tradex_trading.execution.trading_cache import TradingCache
from tradex_trading.reactive.bus import ReactiveBus


def _instrument() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _request(correlation_id: str | None = None) -> OrderRequest:
    return OrderRequest(
        instrument=_instrument(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500")),
        time_in_force=TimeInForce.DAY,
        correlation_id=(
            CorrelationId(correlation_id) if correlation_id is not None else None
        ),
    )


def _bracket_request(correlation_id: str | None = None) -> BracketOrderRequest:
    return BracketOrderRequest(
        instrument=_instrument(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500")),
        time_in_force=TimeInForce.DAY,
        stop_loss_price=Price(value=Decimal("2400")),
        target_price=Price(value=Decimal("2600")),
        correlation_id=(
            CorrelationId(correlation_id) if correlation_id is not None else None
        ),
    )


class _VenueBroker:
    """Recording broker: ACKs submits, records cancels, can fail cancels."""

    owns_position_projection = False

    def __init__(self) -> None:
        self.submitted: list[OrderRequest] = []
        self.cancelled: list[str] = []
        self.cancel_error: Exception | None = None
        self.oms_status_at_cancel: list[OrderStatus] = []
        self.super_cancelled: list[str] = []
        self.engine: ExecutionEngine | None = None

    def submit_order(self, request: OrderRequest) -> object:
        self.submitted.append(request)
        return "venue-1"

    def submit_super_order(self, request: BracketOrderRequest) -> object:
        self.submitted.append(request)
        return "venue-super-1"

    def cancel_order(self, order_id: OrderId) -> None:
        if self.engine is not None:
            cached = self.engine.cache.get_order(order_id.value)
            self.oms_status_at_cancel.append(
                cached.status if cached is not None else OrderStatus.UNKNOWN
            )
        if self.cancel_error is not None:
            raise self.cancel_error
        self.cancelled.append(order_id.value)

    def cancel_super_order(self, order_id: OrderId) -> None:
        if self.cancel_error is not None:
            raise self.cancel_error
        self.super_cancelled.append(order_id.value)


class _AckOnlyFillSource:
    """Paper/simulated-style source: ACKs, no venue, no-op cancel."""

    def __init__(self) -> None:
        self.position_projection_owned = False

    def submit(self, request: OrderRequest) -> tuple[Order, None]:
        return (
            Order(
                order_id=OrderId(value=str(uuid.uuid4())),
                instrument=request.instrument,
                side=request.side,
                order_type=request.order_type,
                quantity=request.quantity,
                price=request.price,
                time_in_force=request.time_in_force,
                status=OrderStatus.ACK,
                correlation_id=request.correlation_id,
            ),
            None,
        )

    def cancel(self, order_id: OrderId) -> None:
        pass


def _build_engine(fill_source: object) -> ExecutionEngine:
    return ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=fill_source,  # type: ignore[arg-type]
        cache=TradingCache(),
        idempotency_guard=MemoryIdempotencyGuard(),
    )


def test_plain_cancel_reaches_venue_exactly_once_before_oms() -> None:
    """N1 contract 1: venue first, exactly once, OMS still ACK at venue time."""
    broker = _VenueBroker()
    engine = _build_engine(BrokerFillSource(broker))
    broker.engine = engine
    receipt = engine.submit(_request(correlation_id="cid-1"))
    assert receipt.status is OrderStatus.ACK

    engine.cancel(receipt.order_id)

    assert broker.cancelled == ["venue-1"]
    assert broker.oms_status_at_cancel == [OrderStatus.ACK], (
        "venue must be called before the OMS flips to CANCELLED"
    )
    cached = engine.cache.get_order(receipt.order_id.value)
    assert cached is not None and cached.status is OrderStatus.CANCELLED


def test_venue_failure_leaves_oms_ack_and_publishes_nothing() -> None:
    """N1 contract 2: a venue reject must not flip the OMS or emit the event."""
    broker = _VenueBroker()
    broker.cancel_error = OrderRejectedError("venue rejected cancel")
    engine = _build_engine(BrokerFillSource(broker))
    broker.engine = engine
    cancelled_events: list[OrderCancelled] = []
    engine._bus.of_type(OrderCancelled).subscribe(on_next=cancelled_events.append)

    receipt = engine.submit(_request(correlation_id="cid-2"))
    with pytest.raises(OrderRejectedError, match="venue rejected"):
        engine.cancel(receipt.order_id)

    cached = engine.cache.get_order(receipt.order_id.value)
    assert cached is not None and cached.status is OrderStatus.ACK
    assert cancelled_events == []
    assert broker.cancelled == []


def test_bracket_cancel_uses_super_endpoint_once() -> None:
    """N1 contract 3: brackets keep venue-first cancel_super_order, never plain."""
    broker = _VenueBroker()
    engine = _build_engine(BrokerFillSource(broker))
    receipt = engine.submit(_bracket_request(correlation_id="cid-3"))
    assert receipt.status is OrderStatus.ACK

    engine.cancel(receipt.order_id)

    assert broker.super_cancelled == ["venue-super-1"]
    assert broker.cancelled == []
    cached = engine.cache.get_order(receipt.order_id.value)
    assert cached is not None and cached.status is OrderStatus.CANCELLED


def test_no_venue_source_keeps_oms_local_semantics() -> None:
    """N1 contract 4: paper/simulated cancels are unchanged (OMS + event + cid)."""
    engine = _build_engine(_AckOnlyFillSource())
    cancelled_events: list[OrderCancelled] = []
    engine._bus.of_type(OrderCancelled).subscribe(on_next=cancelled_events.append)

    receipt = engine.submit(_request(correlation_id="cid-4"))
    engine.cancel(receipt.order_id)

    cached = engine.cache.get_order(receipt.order_id.value)
    assert cached is not None and cached.status is OrderStatus.CANCELLED
    assert len(cancelled_events) == 1
    # The completed submission replays (IdempotencyDuplicate) instead of
    # raising "already reserved" — the cancellation must not leak a
    # reservation into the guard's reserved set.
    result = engine._guard.check_and_reserve(CorrelationId("cid-4"))  # noqa: SLF001
    assert isinstance(result, IdempotencyDuplicate)


def test_route_surfaces_venue_failure_as_http_error() -> None:
    """N1 contract 5: DELETE /orders/{id} returns a typed 4xx, not success."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    from tradex_trading.interface.routes.orders import cancel_order as route_cancel

    broker = _VenueBroker()
    broker.cancel_error = OrderRejectedError("venue rejected cancel")
    engine = _build_engine(BrokerFillSource(broker))
    broker.engine = engine
    receipt = engine.submit(_request(correlation_id="cid-5"))
    session = SimpleNamespace(engine=engine)

    with pytest.raises(HTTPException) as excinfo:
        import asyncio

        asyncio.run(
            route_cancel(receipt.order_id.value, session=session, idempotency_key="k-5")
        )
    assert excinfo.value.status_code == 400
    assert "venue rejected" in str(excinfo.value.detail)