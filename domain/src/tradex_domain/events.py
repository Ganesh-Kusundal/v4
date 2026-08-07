"""Domain event types for the reactive bus.

Typed events that flow through the ReactiveBus as Observable emissions.
All events inherit from ``DomainEvent`` which provides ``timestamp`` and
``correlation_id`` via ``kw_only=True`` (Python 3.10+).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from tradex_domain.execution import Fill, Order, OrderRequest, Position
from tradex_domain.market import Candle, Quote
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
class OrderCancelled(DomainEvent):
    order: Order


@dataclass(frozen=True, slots=True)
class OrderRejected(DomainEvent):
    order: Order
    reason: str = ""


@dataclass(frozen=True, slots=True)
class PositionUpdated(DomainEvent):
    position: Position


@dataclass(frozen=True, slots=True)
class QuoteReceived(DomainEvent):
    quote: Quote


@dataclass(frozen=True, slots=True)
class CandleReceived(DomainEvent):
    candle: Candle


@dataclass(frozen=True, slots=True)
class ErrorOccurred(DomainEvent):
    error: Exception


@dataclass(frozen=True, slots=True)
class SessionStarted(DomainEvent):
    session_id: str = ""


@dataclass(frozen=True, slots=True)
class SessionStopped(DomainEvent):
    session_id: str = ""


@dataclass(frozen=True, slots=True)
class RiskLimitBreached(DomainEvent):
    reason: str = ""
    limit: str = ""


@dataclass(frozen=True, slots=True)
class KillSwitchTripped(DomainEvent):
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ReconciliationDrift(DomainEvent):
    drift_type: str = ""
    details: str = ""


@dataclass(frozen=True, slots=True)
class DataQualityAlert(DomainEvent):
    alert_type: str = ""
    message: str = ""


@dataclass(frozen=True, slots=True)
class PlaceOrderCommand(DomainEvent):
    """CQRS command — strategies publish this instead of calling broker directly."""

    request: OrderRequest


__all__ = [
    "CandleReceived",
    "DataQualityAlert",
    "DomainEvent",
    "ErrorOccurred",
    "KillSwitchTripped",
    "OrderCancelled",
    "OrderFilled",
    "OrderPlaced",
    "OrderRejected",
    "PlaceOrderCommand",
    "PositionUpdated",
    "QuoteReceived",
    "ReconciliationDrift",
    "RiskLimitBreached",
    "SessionStarted",
    "SessionStopped",
]
