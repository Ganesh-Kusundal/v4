"""Gap test for OrderManager — apply_unknown."""

from __future__ import annotations

from decimal import Decimal

from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.execution import Order
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity

from tradex_trading.execution.order_manager import OrderManager
from tradex_trading.execution.trading_cache import TradingCache


def test_order_manager_apply_unknown() -> None:
    """apply_unknown writes order to cache with its current status."""
    cache = TradingCache()
    om = OrderManager(cache)
    instrument = Equity.of("NSE", "TEST")
    order = Order(
        order_id=OrderId(value="unknown-oid"),
        instrument=instrument,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
        status=OrderStatus.UNKNOWN,
    )
    om.apply_unknown(order)
    cached = cache.get_order("unknown-oid")
    assert cached is not None
    assert cached.status == OrderStatus.UNKNOWN
