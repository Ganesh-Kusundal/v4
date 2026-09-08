"""Domain event types for the reactive bus.

Typed events that flow through the ReactiveBus as Observable emissions.
All events inherit from ``DomainEvent`` which provides ``timestamp`` and
``correlation_id`` via ``kw_only=True`` (Python 3.10+).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from tradex_domain.execution import Fill, Order, OrderRequest, Position
from tradex_domain.instruments import Instrument
from tradex_domain.market import Quote
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
class PositionUpdated(DomainEvent):
    """Position accounting or mark-to-market state changed."""

    position: Position
    quote: Quote | None = None


@dataclass(frozen=True, slots=True)
class ErrorOccurred(DomainEvent):
    error: Exception


@dataclass(frozen=True, slots=True)
class OrderCancelled(DomainEvent):
    """An open order was cancelled (post-transition state)."""

    order: Order


@dataclass(frozen=True, slots=True)
class OrderModified(DomainEvent):
    """An open order was modified — carries the post-modification state."""

    order: Order


@dataclass(frozen=True, slots=True)
class PlaceOrderCommand(DomainEvent):
    """CQRS command — strategies publish this instead of calling broker directly."""

    request: OrderRequest


@dataclass(frozen=True, slots=True)
class StaleFeed:
    instrument: Instrument
    age_seconds: float
    last_timestamp: datetime | None


__all__ = [
    "DomainEvent",
    "ErrorOccurred",
    "OrderCancelled",
    "OrderFilled",
    "OrderModified",
    "OrderPlaced",
    "OrderRejected",
    "PlaceOrderCommand",
    "PositionUpdated",
    "StaleFeed",
]
