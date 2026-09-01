"""Order Book Actor — single writer, no locks, event-sourced."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

from tradex_domain.enums import OrderSide
from tradex_domain.execution import Fill, OrderRequest, Position
from tradex_domain.instruments import Equity, Instrument
from tradex_domain.value_objects import Money, OrderId, Price, Quantity

from tradex_trading.events.order_fsm import InvalidStateTransition, OrderState, TERMINAL_STATES, transition
from tradex_trading.events.store import Event, EventStore
from tradex_trading.execution.position_math import apply_fill


@dataclass
class PlaceOrderCommand:
    request: OrderRequest
    correlation_id: str
    event_time: datetime


@dataclass
class ApplyFillCommand:
    order_id: str
    cumulative_filled: Decimal
    fill_price: Decimal
    fill_id: Optional[str]
    correlation_id: str
    event_time: datetime


@dataclass
class CancelOrderCommand:
    order_id: str
    correlation_id: str
    event_time: datetime


@dataclass
class TripKillSwitchCommand:
    reason: str
    correlation_id: str
    event_time: datetime


@dataclass
class _OrderState:
    """Internal order state (not exposed directly)."""

    order_id: str
    instrument: str
    side: str
    quantity: Decimal
    price: Optional[Decimal]
    status: OrderState
    filled_quantity: Decimal
    correlation_id: str


@dataclass
class IdempotencyEntry:
    """Tracks the result of processing a command by correlation_id."""

    events: list[Event]


def _instrument_from_str(instrument_str: str) -> Instrument:
    """Reconstruct an Instrument from its string representation.

    Only equity instruments are supported (NSE:RELIANCE).
    """
    parts = instrument_str.split(":")
    if len(parts) == 2:
        return Equity.of(parts[0], parts[1])
    raise ValueError(f"Cannot reconstruct instrument from: {instrument_str}")


class OrderBookActor:
    """
    Single-writer actor that owns all order/position state.

    All mutations go through handle(). No other code mutates state.
    Events are appended to the EventStore. State can be recovered by replaying events.
    """

    def __init__(self, session_id: str, event_store: EventStore):
        self._session_id = session_id
        self._store = event_store
        self._orders: dict[str, _OrderState] = {}
        self._positions: dict[str, Position] = {}
        self._kill_switch = False
        self._idempotency: dict[str, IdempotencyEntry] = {}

    def handle(self, command) -> list[Event]:
        """Process a command and return events to append."""
        correlation_id = command.correlation_id

        # Idempotency check — return cached result if already processed
        if correlation_id in self._idempotency:
            return self._idempotency[correlation_id].events

        if isinstance(command, PlaceOrderCommand):
            events = self._handle_place_order(command)
        elif isinstance(command, ApplyFillCommand):
            events = self._handle_apply_fill(command)
        elif isinstance(command, CancelOrderCommand):
            events = self._handle_cancel_order(command)
        elif isinstance(command, TripKillSwitchCommand):
            events = self._handle_trip_kill_switch(command)
        else:
            raise ValueError(f"Unknown command type: {type(command)}")

        # Store idempotency entry for successful commands (not rejections)
        if events and events[0].type != "OrderRejected":
            self._idempotency[correlation_id] = IdempotencyEntry(events=events)

        return events

    def _handle_place_order(self, cmd: PlaceOrderCommand) -> list[Event]:
        now = cmd.event_time

        # Kill switch check — reject any new orders when active
        if self._kill_switch:
            event = Event(
                event_id=str(uuid.uuid4()),
                event_time=now,
                processed_time=now,
                correlation_id=cmd.correlation_id,
                session_id=self._session_id,
                type="OrderRejected",
                payload={
                    "correlation_id": cmd.correlation_id,
                    "reason": "kill_switch_active",
                    "order_id": None,
                },
            )
            stored = self._store.append(event)
            return [stored]

        # Create order with NEW status (not yet registered in state)
        order_id = str(uuid.uuid4())
        order = _OrderState(
            order_id=order_id,
            instrument=str(cmd.request.instrument.instrument_id),
            side=cmd.request.side.value,
            quantity=cmd.request.quantity.value,
            price=cmd.request.price.value if cmd.request.price else None,
            status=OrderState.NEW,
            filled_quantity=Decimal("0"),
            correlation_id=cmd.correlation_id,
        )

        # Emit OrderPlaced event BEFORE mutating state
        event = Event(
            event_id=str(uuid.uuid4()),
            event_time=now,
            processed_time=now,
            correlation_id=cmd.correlation_id,
            session_id=self._session_id,
            type="OrderPlaced",
            payload={
                "order_id": order_id,
                "instrument": order.instrument,
                "side": order.side,
                "quantity": str(order.quantity),
                "price": str(order.price) if order.price else None,
                "correlation_id": cmd.correlation_id,
            },
        )
        # Persist FIRST — only mutate state if append succeeds
        stored = self._store.append(event)

        # NOW register order and transition (state mutation AFTER persist)
        self._orders[order_id] = order
        order.status = transition(order.status, "ack")

        return [stored]

    def _handle_apply_fill(self, cmd: ApplyFillCommand) -> list[Event]:
        now = cmd.event_time
        events: list[Event] = []

        order = self._orders.get(cmd.order_id)
        if order is None:
            # Unknown order — create synthetic record for audit trail
            order = _OrderState(
                order_id=cmd.order_id,
                instrument="unknown",
                side="UNKNOWN",
                quantity=cmd.cumulative_filled,
                price=None,
                status=OrderState.ACK,
                filled_quantity=Decimal("0"),
                correlation_id=cmd.correlation_id,
            )

            synth_event = Event(
                event_id=str(uuid.uuid4()),
                event_time=now,
                processed_time=now,
                correlation_id=cmd.correlation_id,
                session_id=self._session_id,
                type="SyntheticOrderCreated",
                payload={
                    "order_id": cmd.order_id,
                    "reason": "fill_for_unknown_order",
                },
            )
            # Persist FIRST — only mutate state if append succeeds
            stored = self._store.append(synth_event)
            events.append(stored)
            # NOW register synthetic order in state (AFTER persist)
            self._orders[cmd.order_id] = order

        # Compute delta vs current filled_quantity (idempotent)
        delta = cmd.cumulative_filled - order.filled_quantity
        if delta <= 0:
            return []  # Duplicate or stale fill — no events

        # Compute new state values BEFORE mutating
        new_filled_quantity = cmd.cumulative_filled
        is_complete = cmd.cumulative_filled >= order.quantity

        # Compute new status via FSM (may raise InvalidStateTransition)
        event_type = "full_fill" if is_complete else "fill"
        try:
            new_status = transition(order.status, event_type)
        except InvalidStateTransition:
            # Fallback for edge cases (e.g., fill from NEW directly)
            new_status = OrderState.FILLED if is_complete else OrderState.PARTIALLY_FILLED

        # Emit OrderFilled event
        filled_event = Event(
            event_id=str(uuid.uuid4()),
            event_time=now,
            processed_time=now,
            correlation_id=cmd.correlation_id,
            session_id=self._session_id,
            type="OrderFilled",
            payload={
                "order_id": order.order_id,
                "instrument": order.instrument,
                "side": order.side,
                "fill_quantity": str(delta),
                "cumulative_filled": str(cmd.cumulative_filled),
                "fill_price": str(cmd.fill_price),
                "is_complete": is_complete,
                "fill_id": cmd.fill_id,
            },
        )
        # Persist FIRST
        stored = self._store.append(filled_event)
        events.append(stored)

        # NOW mutate order state (AFTER persist succeeds)
        order.filled_quantity = new_filled_quantity
        order.status = new_status

        # Update position (skip for unknown instruments)
        instrument_str = order.instrument
        if instrument_str == "unknown":
            return events

        existing_pos = self._positions.get(instrument_str)
        instrument = _instrument_from_str(instrument_str)

        # Determine side for position update
        try:
            side = OrderSide(order.side)
        except ValueError:
            side = OrderSide.BUY  # Default for synthetic/unknown sides

        fill = Fill(
            order_id=OrderId(value=cmd.order_id),
            instrument=instrument,
            side=side,
            quantity=Quantity(value=delta),
            price=Price(value=cmd.fill_price),
            fill_id=cmd.fill_id,
        )
        new_pos = apply_fill(existing_pos, fill)

        # Emit PositionUpdated event
        position_event = Event(
            event_id=str(uuid.uuid4()),
            event_time=now,
            processed_time=now,
            correlation_id=cmd.correlation_id,
            session_id=self._session_id,
            type="PositionUpdated",
            payload={
                "instrument": instrument_str,
                "net_quantity": str(new_pos.quantity.value),
                "avg_price": str(new_pos.avg_price.value),
                "realized_pnl": str(new_pos.realized_pnl.amount),
            },
        )
        # Persist FIRST
        stored = self._store.append(position_event)
        events.append(stored)

        # NOW mutate position state (AFTER persist succeeds)
        self._positions[instrument_str] = new_pos

        return events

    def _handle_cancel_order(self, cmd: CancelOrderCommand) -> list[Event]:
        now = cmd.event_time
        order = self._orders.get(cmd.order_id)
        if order is None:
            return []

        # Cannot cancel an already-terminal order
        if order.status in TERMINAL_STATES:
            return []

        # Compute new status BEFORE mutating
        new_status = transition(order.status, "cancel")

        event = Event(
            event_id=str(uuid.uuid4()),
            event_time=now,
            processed_time=now,
            correlation_id=cmd.correlation_id,
            session_id=self._session_id,
            type="OrderCancelled",
            payload={
                "order_id": cmd.order_id,
                "reason": "user_cancel",
            },
        )
        # Persist FIRST
        stored = self._store.append(event)
        # NOW mutate state (AFTER persist succeeds)
        order.status = new_status
        return [stored]

    def _handle_trip_kill_switch(self, cmd: TripKillSwitchCommand) -> list[Event]:
        now = cmd.event_time

        events: list[Event] = []

        # Emit KillSwitchTripped event
        kill_event = Event(
            event_id=str(uuid.uuid4()),
            event_time=now,
            processed_time=now,
            correlation_id=cmd.correlation_id,
            session_id=self._session_id,
            type="KillSwitchTripped",
            payload={
                "reason": cmd.reason,
                "tripped_at": now.isoformat(),
            },
        )
        stored = self._store.append(kill_event)
        events.append(stored)

        # NOW mutate kill_switch state (AFTER persist succeeds)
        self._kill_switch = True

        # Cancel all open (non-terminal) orders
        for order in self._orders.values():
            if order.status not in TERMINAL_STATES:
                # Compute new status BEFORE mutating
                new_status = transition(order.status, "cancel")
                cancel_event = Event(
                    event_id=str(uuid.uuid4()),
                    event_time=now,
                    processed_time=now,
                    correlation_id=cmd.correlation_id,
                    session_id=self._session_id,
                    type="OrderCancelled",
                    payload={
                        "order_id": order.order_id,
                        "reason": "kill_switch",
                    },
                )
                # Persist FIRST
                stored = self._store.append(cancel_event)
                events.append(stored)
                # NOW mutate state (AFTER persist succeeds)
                order.status = new_status

        return events

    def trip_kill_switch(self, reason: str) -> None:
        """Convenience method for testing — trips kill switch without emitting events."""
        self._kill_switch = True

    def recover(self) -> None:
        """Recover state from event log by replaying all events.

        Rebuilds both state AND the idempotency map so that commands
        with previously-seen correlation_ids are not re-processed.
        """
        events = self._store.read_all(self._session_id)
        for event in events:
            self._apply_event(event)
        # Rebuild idempotency map from all events
        self._rebuild_idempotency(events)

    def _apply_event(self, event: Event) -> None:
        """Apply an event to rebuild state (no store write)."""
        if event.type == "OrderPlaced":
            order = _OrderState(
                order_id=event.payload["order_id"],
                instrument=event.payload["instrument"],
                side=event.payload["side"],
                quantity=Decimal(event.payload["quantity"]),
                price=Decimal(event.payload["price"]) if event.payload.get("price") else None,
                status=OrderState.ACK,
                filled_quantity=Decimal("0"),
                correlation_id=event.payload.get("correlation_id", ""),
            )
            self._orders[order.order_id] = order
        elif event.type == "SyntheticOrderCreated":
            # Register synthetic order in state during recovery
            order = _OrderState(
                order_id=event.payload["order_id"],
                instrument="unknown",
                side="UNKNOWN",
                quantity=Decimal("0"),
                price=None,
                status=OrderState.ACK,
                filled_quantity=Decimal("0"),
                correlation_id=event.correlation_id,
            )
            self._orders[order.order_id] = order
        elif event.type == "OrderFilled":
            order = self._orders.get(event.payload["order_id"])
            if order:
                order.filled_quantity = Decimal(event.payload["cumulative_filled"])
                if event.payload["is_complete"]:
                    order.status = OrderState.FILLED
                else:
                    order.status = OrderState.PARTIALLY_FILLED
        elif event.type == "OrderCancelled":
            order = self._orders.get(event.payload["order_id"])
            if order:
                order.status = OrderState.CANCELLED
        elif event.type == "PositionUpdated":
            instrument_str = event.payload["instrument"]
            self._positions[instrument_str] = Position(
                instrument=_instrument_from_str(instrument_str),
                quantity=Quantity(value=Decimal(event.payload["net_quantity"])),
                avg_price=Price(value=Decimal(event.payload["avg_price"])),
                realized_pnl=Money(amount=Decimal(event.payload["realized_pnl"])),
                unrealized_pnl=Money(amount=Decimal("0")),
            )
        elif event.type == "KillSwitchTripped":
            self._kill_switch = True

    def _rebuild_idempotency(self, events: list[Event]) -> None:
        """Rebuild the idempotency map from replayed events.

        Groups events by correlation_id so that re-processing a command
        with the same correlation_id returns the cached events instead
        of creating duplicates.
        """
        # Group events by correlation_id, preserving order
        grouped: dict[str, list[Event]] = {}
        for event in events:
            cid = event.correlation_id
            if cid not in grouped:
                grouped[cid] = []
            grouped[cid].append(event)

        # Only register entries that represent successful commands
        # (i.e., not OrderRejected)
        for cid, evts in grouped.items():
            if evts[0].type != "OrderRejected":
                self._idempotency[cid] = IdempotencyEntry(events=evts)

    def snapshot(self) -> dict:
        """Return current state (orders, positions, kill_switch)."""
        orders = {}
        for oid, o in self._orders.items():
            orders[oid] = {
                "order_id": o.order_id,
                "instrument": o.instrument,
                "side": o.side,
                "quantity": str(o.quantity),
                "price": str(o.price) if o.price else None,
                "status": o.status.value,
                "filled_quantity": str(o.filled_quantity),
                "correlation_id": o.correlation_id,
            }

        positions = {}
        for inst, pos in self._positions.items():
            positions[inst] = {
                "quantity": str(pos.quantity.value),
                "avg_price": str(pos.avg_price.value),
                "realized_pnl": str(pos.realized_pnl.amount),
            }

        return {
            "orders": orders,
            "positions": positions,
            "kill_switch": self._kill_switch,
        }
