"""Single order construction path.

Eliminates 8+ duplicate Order() construction sites across the codebase.
"""
from decimal import Decimal
from typing import Optional
from tradex_domain.enums import OrderStatus, TimeInForce
from tradex_domain.execution import Order, OrderRequest
from tradex_domain.value_objects import OrderId, Price, Quantity


def create_order(
    request: OrderRequest,
    order_id: str,
    *,
    filled: Decimal = Decimal("0"),
    avg_price: Optional[Decimal] = None,
    status: OrderStatus = OrderStatus.NEW,
) -> Order:
    """Create an Order from an OrderRequest.

    Args:
        request: The order request containing instrument, side, quantity, etc.
        order_id: Unique order identifier.
        filled: Filled quantity (default 0).
        avg_price: Average fill price (default None).
        status: Order status (default NEW).

    Returns:
        A new Order instance.
    """
    return Order(
        order_id=OrderId(value=order_id),
        instrument=request.instrument,
        side=request.side,
        quantity=request.quantity,
        filled_quantity=Quantity(value=filled),
        avg_price_traded=Price(value=avg_price) if avg_price is not None else None,
        order_type=request.order_type,
        price=request.price,
        trigger_price=request.trigger_price,
        stop_loss_price=request.stop_loss_price,
        target_price=request.target_price,
        product_type=request.product_type,
        time_in_force=TimeInForce.DAY,
        status=status,
    )
