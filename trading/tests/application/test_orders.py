"""Integration tests for the application layer order handlers (Wave C2).

Uses real ExecutionEngine + SimulatedFillSource — no mocks.
Tests verify the application contract: correct delegation to the engine,
broker capability gate, and idempotency replay pass-through.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from tradex_domain import (
    CorrelationId,
    Equity,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Price,
    Quantity,
)
from tradex_domain.errors import OrderRejectedError
from tradex_domain.execution import BracketOrderRequest, Order
from tradex_domain.enums import TimeInForce
from tradex_domain.value_objects import OrderId

from tradex_trading.application.orders import (
    BrokerCapabilityError,
    cancel_order,
    modify_order,
    submit_bracket_order,
    submit_order,
)
from tradex_trading.execution.engine import ExecutionEngine
from tradex_trading.execution.fill_sources import SimulatedFillSource
from tradex_trading.reactive.bus import ReactiveBus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _equity() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _engine() -> ExecutionEngine:
    return ExecutionEngine(bus=ReactiveBus(), fill_source=SimulatedFillSource())


def _submit_request(cid: str | None = None) -> OrderRequest:
    return OrderRequest(
        instrument=_equity(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500")),
        correlation_id=CorrelationId(value=cid) if cid else None,
    )


def _bracket_request(cid: str | None = None) -> BracketOrderRequest:
    return BracketOrderRequest(
        instrument=_equity(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500")),
        stop_loss_price=Price(value=Decimal("2400")),
        target_price=Price(value=Decimal("2600")),
        correlation_id=CorrelationId(value=cid) if cid else None,
    )


def _seed_ack_order(engine: ExecutionEngine, oid: str = "order-1") -> Order:
    """Seed an ACK'd (open) order directly into the engine cache."""
    order = Order(
        order_id=OrderId(value=oid),
        instrument=_equity(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500")),
        time_in_force=TimeInForce.DAY,
        status=OrderStatus.ACK,
    )
    engine.cache.update_order(order)
    return order


# ---------------------------------------------------------------------------
# submit_order
# ---------------------------------------------------------------------------

def test_submit_order_returns_receipt_on_success():
    engine = _engine()
    receipt = submit_order(engine, _submit_request())
    assert receipt.status is OrderStatus.FILLED


def test_submit_order_delegates_to_engine():
    """Application handler must reach the engine — not a no-op."""
    engine = _engine()
    cid = "submit-delegate-1"
    receipt = submit_order(engine, _submit_request(cid))
    # Engine recorded the order in its cache.
    all_ids = [o.order_id.value for o in engine.all_orders()]
    assert receipt.order_id.value in all_ids


def test_submit_order_idempotency_replay_returns_order_id():
    """Second call with same correlation id returns the original OrderId."""
    from tradex_trading.execution.idempotency import MemoryIdempotencyGuard

    guard = MemoryIdempotencyGuard()
    engine = ExecutionEngine(
        bus=ReactiveBus(), fill_source=SimulatedFillSource(), idempotency_guard=guard
    )
    cid = "submit-idem-1"
    first = submit_order(engine, _submit_request(cid))
    second = submit_order(engine, _submit_request(cid))
    # First is a receipt; second is the replayed OrderId.
    assert hasattr(first, "order_id")
    assert isinstance(second, OrderId)
    assert second.value == first.order_id.value


# ---------------------------------------------------------------------------
# submit_bracket_order — broker capability gate
# ---------------------------------------------------------------------------

def test_submit_bracket_order_raises_when_no_capabilities():
    engine = _engine()
    broker = SimpleNamespace()  # no 'capabilities' attribute
    with pytest.raises(BrokerCapabilityError):
        submit_bracket_order(engine, broker, _bracket_request("b-nocaps"))


def test_submit_bracket_order_raises_when_capability_false():
    engine = _engine()
    broker = SimpleNamespace(capabilities=SimpleNamespace(supports_super_order=False))
    with pytest.raises(BrokerCapabilityError):
        submit_bracket_order(engine, broker, _bracket_request("b-false"))


def test_submit_bracket_order_succeeds_when_capable():
    engine = _engine()
    broker = SimpleNamespace(capabilities=SimpleNamespace(supports_super_order=True))
    receipt = submit_bracket_order(engine, broker, _bracket_request("b-ok"))
    assert receipt.status is OrderStatus.FILLED


def test_submit_bracket_order_engine_receives_bracket_request():
    """Engine must receive a BracketOrderRequest (not downcast)."""
    received = []
    engine = _engine()
    engine._bus.subscribe(lambda e: received.append(e))  # noqa: SLF001 — test probe

    broker = SimpleNamespace(capabilities=SimpleNamespace(supports_super_order=True))
    submit_bracket_order(engine, broker, _bracket_request("b-type"))
    # An OrderPlaced event should have been emitted with the bracket order
    from tradex_domain.events import OrderPlaced
    placed = [e for e in received if isinstance(e, OrderPlaced)]
    assert placed, "no OrderPlaced event emitted"
    assert isinstance(placed[0].order.stop_loss_price, Price)
    assert isinstance(placed[0].order.target_price, Price)


# ---------------------------------------------------------------------------
# modify_order
# ---------------------------------------------------------------------------

def test_modify_order_updates_price_in_cache():
    engine = _engine()
    _seed_ack_order(engine, "mod-1")
    oid = OrderId(value="mod-1")
    new_request = OrderRequest(
        instrument=_equity(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2450")),
        correlation_id=CorrelationId(value="mod-cid-1"),
    )
    modified = modify_order(engine, oid, new_request)
    assert modified.price.value == Decimal("2450")
    cached = engine.cache.get_order("mod-1")
    assert cached.price.value == Decimal("2450")


def test_modify_order_updates_quantity_in_cache():
    engine = _engine()
    _seed_ack_order(engine, "mod-qty")
    oid = OrderId(value="mod-qty")
    new_request = OrderRequest(
        instrument=_equity(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("5")),
        price=Price(value=Decimal("2500")),
        correlation_id=CorrelationId(value="mod-cid-qty"),
    )
    modified = modify_order(engine, oid, new_request)
    assert modified.quantity.value == Decimal("5")


def test_modify_nonexistent_order_raises():
    engine = _engine()
    oid = OrderId(value="ghost-order")
    request = OrderRequest(
        instrument=_equity(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500")),
        correlation_id=CorrelationId(value="mod-ghost"),
    )
    with pytest.raises(OrderRejectedError):
        modify_order(engine, oid, request)


# ---------------------------------------------------------------------------
# cancel_order
# ---------------------------------------------------------------------------

def test_cancel_order_transitions_to_cancelled():
    engine = _engine()
    _seed_ack_order(engine, "can-1")
    oid = OrderId(value="can-1")
    cid = CorrelationId(value="cancel-cid-1")
    cancelled = cancel_order(engine, oid, cid)
    assert cancelled.status is OrderStatus.CANCELLED
    cached = engine.cache.get_order("can-1")
    assert cached.status is OrderStatus.CANCELLED


def test_cancel_nonexistent_order_raises():
    engine = _engine()
    oid = OrderId(value="no-such-order")
    cid = CorrelationId(value="cancel-ghost")
    with pytest.raises(OrderRejectedError):
        cancel_order(engine, oid, cid)


def test_cancel_order_idempotency_replay():
    """Cancelling with the same correlation id a second time replays the result."""
    from tradex_trading.execution.idempotency import MemoryIdempotencyGuard

    guard = MemoryIdempotencyGuard()
    engine = ExecutionEngine(
        bus=ReactiveBus(), fill_source=SimulatedFillSource(), idempotency_guard=guard
    )
    _seed_ack_order(engine, "can-idem")
    oid = OrderId(value="can-idem")
    cid = CorrelationId(value="cancel-idem-cid")

    first = cancel_order(engine, oid, cid)
    second = cancel_order(engine, oid, cid)

    assert first.status is OrderStatus.CANCELLED
    assert second.status is OrderStatus.CANCELLED
    # Both calls return consistent results — guard replays the first.
    assert first.order_id == second.order_id
