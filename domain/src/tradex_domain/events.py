"""Domain event types for the reactive bus.

Typed events that flow through the ReactiveBus as Observable emissions.
All events inherit from ``DomainEvent`` which provides ``timestamp`` and
``correlation_id`` via ``kw_only=True`` (Python 3.10+).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from tradex_domain.execution import Fill, Order, OrderRequest
from tradex_domain.market import Candle
from tradex_domain.value_objects import CorrelationId


@dataclass(frozen=True, slots=True, kw_only=True)
class DomainEvent:
    """Base for all domain events. Subclasses carry specific payloads."""

    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    correlation_id: CorrelationId | None = None


@dataclass(frozen=True, slots=True)
class OrderPlaced(DomainEvent):
    order: Order


@dataclass(frozen=True, slots=True)
class OrderFilled(DomainEvent):
    fill: Fill


@dataclass(frozen=True, slots=True)
class OrderRejected(DomainEvent):
    order: Order
    reason: str = ""


@dataclass(frozen=True, slots=True)
class CandleReceived(DomainEvent):
    candle: Candle


@dataclass(frozen=True, slots=True)
class ErrorOccurred(DomainEvent):
    error: Exception


@dataclass(frozen=True, slots=True)
class PlaceOrderCommand(DomainEvent):
    """CQRS command — strategies publish this instead of calling broker directly."""

    request: OrderRequest


__all__ = [
    "CandleReceived",
    "DomainEvent",
    "ErrorOccurred",
    "OrderFilled",
    "OrderPlaced",
    "OrderRejected",
    "PlaceOrderCommand",
]
