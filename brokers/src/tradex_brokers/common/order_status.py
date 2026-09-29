"""Unified order status mapping for all broker adapters."""

from __future__ import annotations

from tradex_domain.enums import OrderStatus

_STATUS_MAP: dict[str, OrderStatus] = {
    "TRADED": OrderStatus.FILLED,
    "COMPLETE": OrderStatus.FILLED,
    "PART_TRADED": OrderStatus.PARTIALLY_FILLED,
    "CANCELLED": OrderStatus.CANCELLED,
    "AMO_CANCELLED": OrderStatus.CANCELLED,
    "EXPIRED": OrderStatus.CANCELLED,
    "REJECTED": OrderStatus.REJECTED,
    "PENDING": OrderStatus.PENDING,
    "TRANSIT": OrderStatus.ACK,
    "TRIGGER_PENDING": OrderStatus.ACK,
    "TRIGGER PENDING": OrderStatus.ACK,
    "AFTER_MARKET_ORDER": OrderStatus.ACK,
    "AFTER_MARKET_ORDER_REQ_RECEIVED": OrderStatus.ACK,
    "OPEN_PENDING": OrderStatus.ACK,
    "OPEN": OrderStatus.ACK,
    "QUEUED": OrderStatus.ACK,
}


def map_order_status(raw: str) -> OrderStatus:
    """Map a raw broker order status string to a domain OrderStatus."""
    key = raw.strip().upper().replace(" ", "_")
    return _STATUS_MAP.get(key, OrderStatus.UNKNOWN)
