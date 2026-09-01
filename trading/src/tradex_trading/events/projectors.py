"""Projectors — derive read models from events.

Read models are derived entirely from the event log and can be rebuilt
at any time by replaying all events. Projectors are read-only: they
never write to the event store.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from tradex_trading.events.store import Event


# =============================================================================
# View types — immutable read models
# =============================================================================

@dataclass(frozen=True, slots=True)
class OrderView:
    """Immutable read model for an order."""

    order_id: str
    instrument: str
    side: str
    quantity: Decimal
    price: Optional[Decimal]
    status: str
    filled_quantity: Decimal
    correlation_id: str


@dataclass(frozen=True, slots=True)
class PositionView:
    """Immutable read model for a position."""

    instrument: str
    quantity: Decimal
    avg_price: Decimal
    realized_pnl: Decimal


# =============================================================================
# OrderBookProjector
# =============================================================================

class OrderBookProjector:
    """Derives order read models from events.

    Stateless between calls — all state lives in the internal read model.
    Can be rebuilt by replaying all events from the log.
    """

    def __init__(self) -> None:
        self._orders: dict[str, OrderView] = {}
        self._kill_switch: bool = False

    def apply(self, event: Event) -> None:
        """Apply an event to the read model. Pure projection — no store writes."""
        if event.type == "OrderPlaced":
            self._apply_order_placed(event)
        elif event.type == "OrderFilled":
            self._apply_order_filled(event)
        elif event.type == "OrderCancelled":
            self._apply_order_cancelled(event)
        elif event.type == "OrderRejected":
            self._apply_order_rejected(event)
        elif event.type == "SyntheticOrderCreated":
            self._apply_synthetic_order(event)
        elif event.type == "KillSwitchTripped":
            self._kill_switch = True

    def get_order(self, order_id: str) -> Optional[OrderView]:
        """Get order by ID. Returns None if not found."""
        return self._orders.get(order_id)

    def get_all_orders(self) -> list[OrderView]:
        """Get all tracked orders."""
        return list(self._orders.values())

    def get_orders_by_status(self, status: str) -> list[OrderView]:
        """Filter orders by status."""
        return [o for o in self._orders.values() if o.status == status]

    def is_kill_switch_tripped(self) -> bool:
        """Return whether the kill switch has been tripped."""
        return self._kill_switch

    # -------------------------------------------------------------------------
    # Internal event handlers
    # -------------------------------------------------------------------------

    def _apply_order_placed(self, event: Event) -> None:
        payload = event.payload
        self._orders[payload["order_id"]] = OrderView(
            order_id=payload["order_id"],
            instrument=payload["instrument"],
            side=payload["side"],
            quantity=Decimal(payload["quantity"]),
            price=Decimal(payload["price"]) if payload.get("price") else None,
            status="ACK",
            filled_quantity=Decimal("0"),
            correlation_id=payload.get("correlation_id", ""),
        )

    def _apply_order_filled(self, event: Event) -> None:
        payload = event.payload
        order_id = payload["order_id"]
        existing = self._orders.get(order_id)
        if existing is None:
            return  # Fill for unknown order — no projection

        new_filled = Decimal(payload["cumulative_filled"])
        new_status = "FILLED" if payload.get("is_complete") else "PARTIALLY_FILLED"

        self._orders[order_id] = OrderView(
            order_id=existing.order_id,
            instrument=existing.instrument,
            side=existing.side,
            quantity=existing.quantity,
            price=existing.price,
            status=new_status,
            filled_quantity=new_filled,
            correlation_id=existing.correlation_id,
        )

    def _apply_order_cancelled(self, event: Event) -> None:
        payload = event.payload
        order_id = payload["order_id"]
        existing = self._orders.get(order_id)
        if existing is None:
            return

        self._orders[order_id] = OrderView(
            order_id=existing.order_id,
            instrument=existing.instrument,
            side=existing.side,
            quantity=existing.quantity,
            price=existing.price,
            status="CANCELLED",
            filled_quantity=existing.filled_quantity,
            correlation_id=existing.correlation_id,
        )

    def _apply_order_rejected(self, event: Event) -> None:
        payload = event.payload
        order_id = payload.get("order_id")
        if order_id is None:
            return  # Rejected before order ID assigned — nothing to track

        self._orders[order_id] = OrderView(
            order_id=order_id,
            instrument="unknown",
            side="UNKNOWN",
            quantity=Decimal("0"),
            price=None,
            status="REJECTED",
            filled_quantity=Decimal("0"),
            correlation_id=payload.get("correlation_id", ""),
        )

    def _apply_synthetic_order(self, event: Event) -> None:
        payload = event.payload
        order_id = payload["order_id"]
        self._orders[order_id] = OrderView(
            order_id=order_id,
            instrument="unknown",
            side="UNKNOWN",
            quantity=Decimal("0"),
            price=None,
            status="ACK",
            filled_quantity=Decimal("0"),
            correlation_id=event.correlation_id,
        )


# =============================================================================
# PositionProjector
# =============================================================================

class PositionProjector:
    """Derives position read models from events."""

    def __init__(self) -> None:
        self._positions: dict[str, PositionView] = {}

    def apply(self, event: Event) -> None:
        """Apply an event to the read model."""
        if event.type == "PositionUpdated":
            payload = event.payload
            self._positions[payload["instrument"]] = PositionView(
                instrument=payload["instrument"],
                quantity=Decimal(payload["net_quantity"]),
                avg_price=Decimal(payload["avg_price"]),
                realized_pnl=Decimal(payload["realized_pnl"]),
            )

    def get_position(self, instrument: str) -> Optional[PositionView]:
        """Get position by instrument. Returns None if not found."""
        return self._positions.get(instrument)

    def get_all_positions(self) -> list[PositionView]:
        """Get all tracked positions."""
        return list(self._positions.values())
