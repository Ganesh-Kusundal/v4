"""Recovery contract tests."""

from __future__ import annotations

from tradex_domain.events import OrderPlaced
from tradex_domain.execution import Order

from tradex_trading.execution.recovery import InMemoryEventStore, SessionRecovery


class _Orders:
    def __init__(self) -> None:
        self.saved: list[Order] = []

    def save(self, order: Order) -> None:
        self.saved.append(order)

    def get(self, order_id: str) -> Order | None:
        return next((o for o in self.saved if o.order_id.value == order_id), None)

    def all(self):
        return tuple(self.saved)


def _order() -> Order:
    from decimal import Decimal

    from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
    from tradex_domain.instruments import Equity
    from tradex_domain.value_objects import OrderId, Price, Quantity

    return Order(
        order_id=OrderId("recovery-1"),
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(Decimal("1")),
        price=Price(Decimal("100")),
        time_in_force=TimeInForce.DAY,
        status=OrderStatus.NEW,
    )


def test_recovery_rebuilds_order_projection_from_events() -> None:
    events = InMemoryEventStore()
    orders = _Orders()
    events.append(OrderPlaced(order=_order()))

    result = SessionRecovery(events, orders).recover()

    assert result.orders_recovered == 1
    assert result.events_replayed == 1
    assert orders.saved[0].order_id.value == "recovery-1"
