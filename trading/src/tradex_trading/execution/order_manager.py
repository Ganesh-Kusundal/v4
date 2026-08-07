"""Order lifecycle FSM — manages order state transitions."""

from __future__ import annotations

import logging

from tradex_domain.enums import OrderStatus
from tradex_domain.execution import Fill, Order
from tradex_domain.value_objects import OrderId, Quantity

from tradex_trading.execution.trading_cache import TradingCache

log = logging.getLogger(__name__)


class OrderManager:
    """Manages order lifecycle transitions and keeps the cache in sync."""

    def __init__(self, cache: TradingCache) -> None:
        self._cache = cache

    def on_order_created(self, order: Order) -> None:
        """Cache a newly created order (status NEW)."""
        log.info("Order %s: NEW (created)", order.order_id)
        self._cache.update_order(order)

    def on_order_ack(self, order: Order) -> None:
        """Transition order to ACK and update cache."""
        old_status = order.status
        acked = order.transition_to(OrderStatus.ACK)
        log.info("Order %s: %s -> %s", order.order_id, old_status, OrderStatus.ACK)
        self._cache.update_order(acked)

    def on_order_filled(self, order: Order, fill: Fill) -> None:
        """Transition order to FILLED, update filled_quantity, and cache."""
        old_status = order.status
        new_filled = Quantity(
            value=order.filled_quantity.value + fill.quantity.value
        )
        new_status = (
            OrderStatus.FILLED
            if new_filled.value >= order.quantity.value
            else OrderStatus.PARTIALLY_FILLED
        )
        filled_order = Order(
            order_id=order.order_id,
            instrument=order.instrument,
            side=order.side,
            order_type=order.order_type,
            quantity=order.quantity,
            price=order.price,
            time_in_force=order.time_in_force,
            status=new_status,
            correlation_id=order.correlation_id,
            trigger_price=order.trigger_price,
            product_type=order.product_type,
            tag=order.tag,
            filled_quantity=new_filled,
            target_price=order.target_price,
            stop_loss_price=order.stop_loss_price,
            trailing_jump=order.trailing_jump,
        )
        self._cache.update_order(filled_order)
        log.info("Order %s: %s -> %s", order.order_id, old_status, new_status)

    def on_order_cancelled(self, order: Order) -> None:
        """Transition order to CANCELLED and update cache."""
        old_status = order.status
        cancelled = order.transition_to(OrderStatus.CANCELLED)
        log.info("Order %s: %s -> %s", order.order_id, old_status, OrderStatus.CANCELLED)
        self._cache.update_order(cancelled)

    def on_order_rejected(self, order: Order, reason: str) -> None:
        """Transition order to REJECTED and update cache."""
        old_status = order.status
        rejected = order.transition_to(OrderStatus.REJECTED)
        log.info(
            "Order %s: %s -> %s (reason: %s)",
            order.order_id, old_status, OrderStatus.REJECTED, reason,
        )
        self._cache.update_order(rejected)

    def get_order(self, order_id: OrderId) -> Order | None:
        """Look up an order by ID."""
        oid = order_id.value if isinstance(order_id, OrderId) else str(order_id)
        return self._cache.get_order(oid)

    def upsert(self, order: Order) -> None:
        """Insert or update an order in the cache."""
        self._cache.update_order(order)

    def apply_unknown(self, order: Order) -> None:
        """Force-write an order with UNKNOWN status (for reconciliation)."""
        self._cache.update_order(order)
        log.info("Order %s: force-written with status %s", order.order_id, order.status)

__all__ = ["OrderManager"]
