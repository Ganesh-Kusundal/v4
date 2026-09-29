"""Tests for order_factory."""
from decimal import Decimal
from tradex_domain.order_factory import create_order
from tradex_domain.enums import OrderSide, OrderType, OrderStatus, TimeInForce
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity


def test_create_order_basic():
    req = OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        quantity=Quantity(value=Decimal("10")),
        order_type=OrderType.MARKET,
    )
    order = create_order(req, order_id="ord-1")
    assert order.order_id.value == "ord-1"
    assert order.instrument.symbol == "RELIANCE"
    assert order.side == OrderSide.BUY
    assert order.quantity.value == Decimal("10")
    assert order.order_type == OrderType.MARKET
    assert order.status == OrderStatus.NEW
    assert order.time_in_force == TimeInForce.DAY
    assert order.filled_quantity.value == Decimal("0")
    assert order.avg_price_traded is None


def test_create_order_with_protective():
    req = OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        quantity=Quantity(value=Decimal("10")),
        order_type=OrderType.MARKET,
        stop_loss_price=Price(value=Decimal("95.00")),
        target_price=Price(value=Decimal("105.00")),
    )
    order = create_order(req, order_id="ord-2")
    assert order.stop_loss_price.value == Decimal("95.00")
    assert order.target_price.value == Decimal("105.00")


def test_create_order_filled():
    req = OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        quantity=Quantity(value=Decimal("10")),
        order_type=OrderType.MARKET,
    )
    order = create_order(req, order_id="ord-3", filled=Decimal("10"), avg_price=Decimal("100.00"), status=OrderStatus.FILLED)
    assert order.filled_quantity.value == Decimal("10")
    assert order.avg_price_traded.value == Decimal("100.00")
    assert order.status == OrderStatus.FILLED
