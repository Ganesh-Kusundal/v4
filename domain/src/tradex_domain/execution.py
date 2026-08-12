"""Execution and portfolio objects (FDS 05 §6.7/§6.8, D-10/D-11/D-13).

Order lifecycle: NEW -> PENDING -> ACK -> PARTIALLY_FILLED -> FILLED
-> CANCELLED -> REJECTED. ``Order.transition_to`` returns a new frozen Order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain.enums import (
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
    TimeInForce,
)
from tradex_domain.errors import SessionStateError
from tradex_domain.instruments import Instrument
from tradex_domain.serialization import Serializable
from tradex_domain.value_objects import AccountId, CorrelationId, Money, OrderId, Price, Quantity

_LEGAL_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.NEW: frozenset({OrderStatus.PENDING, OrderStatus.CANCELLED, OrderStatus.REJECTED}),
    OrderStatus.PENDING: frozenset({OrderStatus.ACK, OrderStatus.CANCELLED, OrderStatus.REJECTED}),
    OrderStatus.ACK: frozenset({
        OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED,
        OrderStatus.CANCELLED, OrderStatus.REJECTED,
    }),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.SUBMITTED: frozenset(
        {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELLED}
    ),
    OrderStatus.UNKNOWN: frozenset(),
}


@dataclass(frozen=True, slots=True)
class OrderRequest(Serializable):
    instrument: Instrument
    side: OrderSide
    order_type: OrderType
    quantity: Quantity
    price: Price | None = None
    trigger_price: Price | None = None
    time_in_force: TimeInForce = TimeInForce.DAY
    product_type: ProductType = ProductType.INTRADAY
    correlation_id: CorrelationId | None = None
    tag: str | None = None
    #: Market-data timestamp that drove this order (signal bar/quote), so fill
    #: sources can stamp deterministic fill timestamps instead of ``now()`` —
    #: reproducible event logs across runs (parity review #5/#8).
    reference_timestamp: datetime | None = None
    disclosed_quantity: int = 0
    market_protection: int = -1
    target_price: Price | None = None
    stop_loss_price: Price | None = None
    trailing_jump: Price | None = None

    def __post_init__(self) -> None:
        if self.quantity.value <= 0:
            raise ValueError("OrderRequest quantity must be positive")
        if self.price is not None and self.price.value < 0:
            raise ValueError("OrderRequest price must be non-negative")
        if self.disclosed_quantity < 0:
            raise ValueError("OrderRequest disclosed_quantity must be non-negative")


@dataclass(frozen=True, slots=True)
class Order(Serializable):
    """Durable order record. Status changes only via ``transition_to``."""

    order_id: OrderId
    instrument: Instrument
    side: OrderSide
    order_type: OrderType
    quantity: Quantity
    price: Price | None
    time_in_force: TimeInForce
    status: OrderStatus
    correlation_id: CorrelationId | None = None
    trigger_price: Price | None = None
    product_type: ProductType = ProductType.INTRADAY
    tag: str | None = None
    filled_quantity: Quantity = field(default_factory=lambda: Quantity(value=Decimal("0")))
    target_price: Price | None = None
    stop_loss_price: Price | None = None
    trailing_jump: Price | None = None

    def transition_to(self, new_status: OrderStatus) -> Order:
        allowed = _LEGAL_TRANSITIONS.get(self.status, frozenset())
        if new_status not in allowed:
            raise SessionStateError(
                f"illegal order transition {self.status} -> {new_status}"
            )
        return Order(
            order_id=self.order_id,
            instrument=self.instrument,
            side=self.side,
            order_type=self.order_type,
            quantity=self.quantity,
            price=self.price,
            time_in_force=self.time_in_force,
            status=new_status,
            correlation_id=self.correlation_id,
            trigger_price=self.trigger_price,
            product_type=self.product_type,
            tag=self.tag,
            target_price=self.target_price,
            stop_loss_price=self.stop_loss_price,
            trailing_jump=self.trailing_jump,
            filled_quantity=self.filled_quantity,
        )

    @property
    def is_active(self) -> bool:
        """True if order is in any active (in-flight) state."""
        return self.status in {
            OrderStatus.NEW, OrderStatus.PENDING, OrderStatus.ACK,
            OrderStatus.PARTIALLY_FILLED, OrderStatus.SUBMITTED,
        }

    @property
    def is_filled(self) -> bool:
        """True if order is fully filled."""
        return self.status == OrderStatus.FILLED

    @property
    def is_cancelled(self) -> bool:
        """True if order is cancelled."""
        return self.status == OrderStatus.CANCELLED

    @property
    def is_pending(self) -> bool:
        """True if order is awaiting acknowledgement."""
        return self.status in {OrderStatus.NEW, OrderStatus.PENDING, OrderStatus.SUBMITTED}

    @property
    def is_terminal(self) -> bool:
        """True if order reached a final state (filled, cancelled, or rejected)."""
        return self.status in {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}

    @property
    def is_rejected(self) -> bool:
        """True if order was rejected."""
        return self.status == OrderStatus.REJECTED

    @property
    def is_partially_filled(self) -> bool:
        """True if order is partially filled."""
        return self.status == OrderStatus.PARTIALLY_FILLED

    @property
    def remaining_quantity(self) -> Decimal:
        """Quantity not yet filled."""
        return self.quantity.value - self.filled_quantity.value


@dataclass(frozen=True, slots=True)
class OrderReceipt(Serializable):
    order_id: OrderId
    status: OrderStatus = OrderStatus.SUBMITTED
    message: str = "submitted"


@dataclass(frozen=True, slots=True)
class OrderResult(Serializable):
    """Provider result of a super/forever-order mutation (modify/cancel/list).

    Returned by ``modify_super_order``/``cancel_super_order``/``list_super_orders``
    and the forever-order equivalents, so adapters and consumers share one
    frozen contract instead of opaque ``dict``s (F-6).
    """

    order_id: OrderId
    status: OrderStatus = OrderStatus.UNKNOWN
    message: str = ""

    def __post_init__(self) -> None:
        # Coerce raw strings (from broker payloads) to OrderStatus.
        if isinstance(self.status, str) and not isinstance(self.status, OrderStatus):
            try:
                object.__setattr__(self, "status", OrderStatus(self.status.upper()))
            except ValueError:
                object.__setattr__(self, "status", OrderStatus.UNKNOWN)


@dataclass(frozen=True, slots=True)
class Fill(Serializable):
    order_id: OrderId
    instrument: Instrument
    side: OrderSide
    quantity: Quantity
    price: Price
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: Exchange/broker trade identifier when the venue provides one (e.g.
    #: Dhan ``tradeId``). Lets the live-fill bridge distinguish two genuine
    #: equal-lot partial fills of the same order from a re-published fill.
    fill_id: str | None = None

    @property
    def is_buy(self) -> bool:
        return self.side == OrderSide.BUY

    @property
    def is_sell(self) -> bool:
        return self.side == OrderSide.SELL

    @property
    def value(self) -> Money:
        """Trade value = price * quantity."""
        return Money(amount=self.price.value * self.quantity.value, currency="INR")


@dataclass(frozen=True, slots=True)
class Position(Serializable):
    instrument: Instrument
    quantity: Quantity
    avg_price: Price
    realized_pnl: Money
    unrealized_pnl: Money

    @property
    def total_pnl(self) -> Money:
        """Total P&L (realized + unrealized)."""
        return Money(
            amount=self.realized_pnl.amount + self.unrealized_pnl.amount,
            currency=self.realized_pnl.currency,
        )

    @property
    def is_long(self) -> bool:
        """True if position quantity is positive."""
        return self.quantity.value > 0

    @property
    def is_short(self) -> bool:
        """True if position quantity is negative."""
        return self.quantity.value < 0

    @property
    def market_value(self) -> Money:
        """Quantity * average price."""
        return Money(
            amount=self.quantity.value * self.avg_price.value,
            currency=self.instrument.currency,
        )


@dataclass(frozen=True, slots=True)
class Account(Serializable):
    account_id: AccountId
    balance: Money | None = None
    margin: Money | None = None
    equity: Money | None = None


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot(Serializable):
    positions: list[Position] = field(default_factory=list)
    account: Account | None = None

    @property
    def total_value(self) -> Money:
        """Sum of all position market values + account equity.

        When account equity is available, it is the authoritative total
        (broker-reported). Position values are used only when account
        equity is absent.
        """
        if self.account is not None and self.account.equity is not None:
            return self.account.equity
        pos_value = sum(
            (p.quantity.value * p.avg_price.value for p in self.positions),
            Decimal("0"),
        )
        currency = self.positions[0].instrument.currency if self.positions else "INR"
        return Money(amount=pos_value, currency=currency)

    @property
    def total_pnl(self) -> Money:
        """Sum of all position total P&L."""
        total = sum(
            (p.total_pnl.amount for p in self.positions),
            Decimal("0"),
        )
        return Money(amount=total, currency="INR")

    @property
    def net_exposure(self) -> Money:
        """Sum of absolute position market values."""
        exposure = sum(
            (abs(p.quantity.value) * p.avg_price.value for p in self.positions),
            Decimal("0"),
        )
        return Money(amount=exposure, currency="INR")

    def __len__(self) -> int:
        return len(self.positions)


__all__ = [
    "Account",
    "Fill",
    "Order",
    "OrderReceipt",
    "OrderRequest",
    "OrderResult",
    "PortfolioSnapshot",
    "Position",
]
