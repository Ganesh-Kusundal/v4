"""Canonical order status set definitions."""
from tradex_domain.enums import OrderStatus

ACTIVE_STATUSES: frozenset[OrderStatus] = frozenset({
    OrderStatus.NEW,
    OrderStatus.PENDING,
    OrderStatus.SUBMITTED,
    OrderStatus.ACK,
    OrderStatus.PARTIALLY_FILLED,
})

PENDING_STATUSES: frozenset[OrderStatus] = frozenset({
    OrderStatus.NEW,
    OrderStatus.PENDING,
    OrderStatus.SUBMITTED,
    OrderStatus.ACK,
})

TERMINAL_STATUSES: frozenset[OrderStatus] = frozenset({
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.REJECTED,
})

__all__ = [
    "ACTIVE_STATUSES",
    "PENDING_STATUSES",
    "TERMINAL_STATUSES",
]
