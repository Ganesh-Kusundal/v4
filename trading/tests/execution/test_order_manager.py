"""Tests for OrderManager — partial fills and state transitions."""

from __future__ import annotations

from decimal import Decimal

from tradex_domain.enums import (
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
    TimeInForce,
)
from tradex_domain.execution import Fill, Order
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity

from tradex_trading.execution.order_manager import OrderManager
from tradex_trading.execution.trading_cache import TradingCache


def _make_order(
    quantity: Decimal = Decimal("100"),
    status: OrderStatus = OrderStatus.NEW,
) -> Order:
    return Order(
        order_id=OrderId(value="test-order-1"),
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=quantity),
        price=Price(value=Decimal("2500.00")),
        time_in_force=TimeInForce.DAY,
        status=status,
        product_type=ProductType.INTRADAY,
    )


def _make_fill(
    order_id: str = "test-order-1",
    quantity: Decimal = Decimal("30"),
    price: Decimal = Decimal("2500.00"),
) -> Fill:
    from datetime import UTC, datetime

    return Fill(
        order_id=OrderId(value=order_id),
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        quantity=Quantity(value=quantity),
        price=Price(value=price),
        timestamp=datetime.now(UTC),
    )


class TestOrderManagerPartialFills:
    """Partial fills set PARTIALLY_FILLED, full fills set FILLED."""

    def test_partial_fill_sets_partially_filled(self) -> None:
        cache = TradingCache()
        mgr = OrderManager(cache)
        order = _make_order(quantity=Decimal("100"))
        mgr.on_order_created(order)

        fill = _make_fill(quantity=Decimal("30"))
        mgr.on_order_filled(order, fill)

        cached = cache.get_order("test-order-1")
        assert cached is not None
        assert cached.status == OrderStatus.PARTIALLY_FILLED
        assert cached.filled_quantity.value == Decimal("30")

    def test_full_fill_sets_filled(self) -> None:
        cache = TradingCache()
        mgr = OrderManager(cache)
        order = _make_order(quantity=Decimal("100"))
        mgr.on_order_created(order)

        fill = _make_fill(quantity=Decimal("100"))
        mgr.on_order_filled(order, fill)

        cached = cache.get_order("test-order-1")
        assert cached is not None
        assert cached.status == OrderStatus.FILLED
        assert cached.filled_quantity.value == Decimal("100")

    def test_multiple_partial_fills(self) -> None:
        cache = TradingCache()
        mgr = OrderManager(cache)
        order = _make_order(quantity=Decimal("100"))
        mgr.on_order_created(order)

        # First partial fill: 30/100
        fill1 = _make_fill(quantity=Decimal("30"))
        mgr.on_order_filled(order, fill1)
        cached = cache.get_order("test-order-1")
        assert cached.status == OrderStatus.PARTIALLY_FILLED

        # Second partial fill: 30+50=80/100
        fill2 = _make_fill(quantity=Decimal("50"))
        updated = cached
        mgr.on_order_filled(updated, fill2)
        cached = cache.get_order("test-order-1")
        assert cached.status == OrderStatus.PARTIALLY_FILLED
        assert cached.filled_quantity.value == Decimal("80")

        # Final fill: 80+20=100/100
        fill3 = _make_fill(quantity=Decimal("20"))
        updated = cached
        mgr.on_order_filled(updated, fill3)
        cached = cache.get_order("test-order-1")
        assert cached.status == OrderStatus.FILLED
        assert cached.filled_quantity.value == Decimal("100")


class TestOrderManagerStateTransitions:
    """Order state transitions via OrderManager."""

    def test_on_order_created_caches_order(self) -> None:
        cache = TradingCache()
        mgr = OrderManager(cache)
        order = _make_order()
        mgr.on_order_created(order)
        assert cache.get_order("test-order-1") is not None

    def test_on_order_cancelled(self) -> None:
        cache = TradingCache()
        mgr = OrderManager(cache)
        order = _make_order(status=OrderStatus.NEW)
        mgr.on_order_created(order)
        mgr.on_order_cancelled(order)
        cached = cache.get_order("test-order-1")
        assert cached is not None
        assert cached.status == OrderStatus.CANCELLED

    def test_on_order_rejected(self) -> None:
        cache = TradingCache()
        mgr = OrderManager(cache)
        order = _make_order(status=OrderStatus.NEW)
        mgr.on_order_created(order)
        mgr.on_order_rejected(order, "risk_check_failed")
        cached = cache.get_order("test-order-1")
        assert cached is not None
        assert cached.status == OrderStatus.REJECTED
