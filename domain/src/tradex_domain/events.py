"""Domain event types for the reactive bus.

Typed events that flow through the ReactiveBus as Observable emissions.
All events inherit from ``DomainEvent`` which provides ``timestamp`` and
``correlation_id`` via ``kw_only=True`` (Python 3.10+).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

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
    """A fill occurred. ``fee_amount`` is the fee actually charged.

    The fee travels with the event because it cannot be re-derived on replay:
    the brokerage cap accrues across a partial-fill sequence, so recomputing
    from a fill in isolation yields a different number than the one charged.
    """

    fill: Fill
    fee_amount: Decimal | None = None


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
class StaleFeed(DomainEvent):
    """Raised when a quote/depth feed has not been updated within the
    configured staleness threshold. Inherits from ``DomainEvent`` so it
    carries ``timestamp`` and ``correlation_id`` (L2 fix — previously
    was a bare dataclass, inconsistent with other domain events)."""

    instrument: Instrument
    age_seconds: float
    last_timestamp: datetime | None = None


@dataclass(frozen=True, slots=True)
class FeedGapDetected(DomainEvent):
    """A feed integrity violation was observed on the live tape.

    ``kind`` is one of the :class:`tradex_trading.runtime.feed_integrity.GapKind`
    values (``duplicate_event``, ``out_of_order``, ``large_jump``,
    ``missing_bar``, ``duplicate_closed_bar``, ``session_boundary``).
    Publishing is observation-only: a gap never blocks the tape, it makes the
    defect visible to operators and downstream consumers.
    """

    instrument: Instrument | None = None
    kind: str = ""
    count: int = 1
    detail: str = ""


@dataclass(frozen=True, slots=True)
class FeedRecovered(DomainEvent):
    """A resynchronising feed completed history replay and is safe to trade.

    Emitted only after the recovery coordinator has replayed subscribed
    instruments from the last accepted event and reconciled the recovered
    closed bars. A bare socket reconnect never reaches this event.
    """

    generation: int = 0
    recovered_through: datetime | None = None
    instruments: tuple[str, ...] = ()
    bars_replayed: int = 0
    missing_bars: int = 0


# ---------------------------------------------------------------------------
# Wave C1 — OMS full event set (appended to durable store, not bus)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class RiskDecision(DomainEvent):
    """Risk gate verdict for a pending order request.

    ``approved=True`` is a pass; ``approved=False`` blocks submission and is
    accompanied by an ``OrderRejected`` event.  ``reason`` is empty on approval.
    """

    request: OrderRequest
    approved: bool = True
    reason: str = ""


@dataclass(frozen=True, slots=True)
class BrokerOrderRequested(DomainEvent):
    """Order dispatched to the broker / fill-source — before venue response."""

    request: OrderRequest


@dataclass(frozen=True, slots=True)
class BrokerOrderAcknowledged(DomainEvent):
    """Broker / fill-source returned an order id — venue accepted the request."""

    order: Order


@dataclass(frozen=True, slots=True)
class CancelRequested(DomainEvent):
    """Cancel intent dispatched to broker before venue confirmation."""

    order_id: str


@dataclass(frozen=True, slots=True)
class UnknownSubmission(DomainEvent):
    """Order crossed the broker boundary; outcome unknown (network fault etc.).

    Emitted when ``FillSource.submission_boundary_crossed`` is set and the
    submission call raises — the order may or may not be live at the venue.
    """

    request: OrderRequest
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ReconciliationResult(DomainEvent):
    """Reconciliation run completed; ``drift_count`` is the number of discrepancies."""

    drift_count: int = 0
    detail: str = ""


@dataclass(frozen=True, slots=True)
class CashAccountInitialized(DomainEvent):
    """Opening cash anchor for an account.

    Cash cannot be folded from fills alone — a ledger needs a starting balance.
    The anchor is written once per account (from broker funds on a cold live
    boot, from configuration in paper/backtest) and every later balance is that
    anchor plus the fills, fees, and adjustments in the log. Its absence means
    cash is *unknown*, which is distinct from zero and must not be guessed.
    """

    amount: Decimal


@dataclass(frozen=True, slots=True)
class CashReconciled(DomainEvent):
    """An accepted correction to cash after comparing against broker funds.

    Written when drift between the folded local balance and the broker's
    reported balance has been acknowledged by an operator, so the fold stays
    consistent with the venue without silently overwriting history.
    """

    previous: Decimal
    amount: Decimal
    reason: str = ""


__all__ = [
    "BrokerOrderAcknowledged",
    "BrokerOrderRequested",
    "CancelRequested",
    "CashAccountInitialized",
    "CashReconciled",
    "DomainEvent",
    "ErrorOccurred",
    "FeedGapDetected",
    "FeedRecovered",
    "OrderCancelled",
    "OrderFilled",
    "OrderModified",
    "OrderPlaced",
    "OrderRejected",
    "PlaceOrderCommand",
    "PositionUpdated",
    "ReconciliationResult",
    "RiskDecision",
    "StaleFeed",
    "UnknownSubmission",
]
