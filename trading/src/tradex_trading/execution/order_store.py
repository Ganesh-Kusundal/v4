"""Order persistence abstraction — extracted from engine.py (PE-6).

Provides the OrderStore protocol and an in-memory implementation for
tests and single-process use. Production deployments use SQLiteOrderStore
from tradex_trading.execution.sqlite_store.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tradex_domain.execution import Order
from tradex_domain.value_objects import OrderId


@runtime_checkable
class OrderStore(Protocol):
    """Persistence abstraction for orders."""

    def upsert(self, order: Order) -> None: ...
    def get(self, order_id: OrderId) -> Order | None: ...
    def all_orders(self) -> list[Order]: ...


class InMemoryOrderStore:
    """Dict-backed OrderStore for tests and single-process use."""

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}

    def upsert(self, order: Order) -> None:
        self._orders[order.order_id.value] = order

    def get(self, order_id: OrderId) -> Order | None:
        key = order_id.value if isinstance(order_id, OrderId) else str(order_id)
        return self._orders.get(key)

    def all_orders(self) -> list[Order]:
        return list(self._orders.values())
