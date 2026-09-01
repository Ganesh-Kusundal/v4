"""Tests for Order Book Projectors — read-model derivation from events."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradex_trading.events.store import Event
from tradex_trading.events.projectors import (
    OrderBookProjector,
    PositionProjector,
    OrderView,
    PositionView,
)


# =============================================================================
# Helpers — build real events (no mocking)
# =============================================================================

def _order_placed_event(
    order_id: str = "ord-001",
    instrument: str = "NSE:RELIANCE",
    side: str = "BUY",
    quantity: str = "10",
    price: str = "2500",
    correlation_id: str = "corr-001",
) -> Event:
    return Event(
        event_id=f"evt-{order_id}-placed",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
        processed_time=datetime(2026, 1, 1, 9, 15, 1, tzinfo=UTC),
        correlation_id=correlation_id,
        session_id="sess-001",
        type="OrderPlaced",
        payload={
            "order_id": order_id,
            "instrument": instrument,
            "side": side,
            "quantity": quantity,
            "price": price,
            "correlation_id": correlation_id,
        },
    )


def _order_filled_event(
    order_id: str,
    instrument: str = "NSE:RELIANCE",
    side: str = "BUY",
    fill_quantity: str = "5",
    cumulative_filled: str = "5",
    fill_price: str = "2500",
    is_complete: bool = False,
    fill_id: str = "trade-001",
    correlation_id: str = "corr-002",
) -> Event:
    return Event(
        event_id=f"evt-{order_id}-filled-{fill_id}",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
        processed_time=datetime(2026, 1, 1, 9, 16, 1, tzinfo=UTC),
        correlation_id=correlation_id,
        session_id="sess-001",
        type="OrderFilled",
        payload={
            "order_id": order_id,
            "instrument": instrument,
            "side": side,
            "fill_quantity": fill_quantity,
            "cumulative_filled": cumulative_filled,
            "fill_price": fill_price,
            "is_complete": is_complete,
            "fill_id": fill_id,
        },
    )


def _order_cancelled_event(
    order_id: str,
    reason: str = "user_cancel",
    correlation_id: str = "corr-003",
) -> Event:
    return Event(
        event_id=f"evt-{order_id}-cancelled",
        event_time=datetime(2026, 1, 1, 9, 17, tzinfo=UTC),
        processed_time=datetime(2026, 1, 1, 9, 17, 1, tzinfo=UTC),
        correlation_id=correlation_id,
        session_id="sess-001",
        type="OrderCancelled",
        payload={
            "order_id": order_id,
            "reason": reason,
        },
    )


def _order_rejected_event(
    order_id: str | None = None,
    reason: str = "kill_switch_active",
    correlation_id: str = "corr-004",
) -> Event:
    return Event(
        event_id=f"evt-rejected-{correlation_id}",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
        processed_time=datetime(2026, 1, 1, 9, 15, 1, tzinfo=UTC),
        correlation_id=correlation_id,
        session_id="sess-001",
        type="OrderRejected",
        payload={
            "correlation_id": correlation_id,
            "reason": reason,
            "order_id": order_id,
        },
    )


def _position_updated_event(
    instrument: str = "NSE:RELIANCE",
    net_quantity: str = "5",
    avg_price: str = "2500",
    realized_pnl: str = "0",
    correlation_id: str = "corr-002",
) -> Event:
    return Event(
        event_id=f"evt-pos-{instrument}-{correlation_id}",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
        processed_time=datetime(2026, 1, 1, 9, 16, 1, tzinfo=UTC),
        correlation_id=correlation_id,
        session_id="sess-001",
        type="PositionUpdated",
        payload={
            "instrument": instrument,
            "net_quantity": net_quantity,
            "avg_price": avg_price,
            "realized_pnl": realized_pnl,
        },
    )


def _synthetic_order_created_event(
    order_id: str = "ext-ord-001",
    correlation_id: str = "corr-ext",
) -> Event:
    return Event(
        event_id=f"evt-synth-{order_id}",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
        processed_time=datetime(2026, 1, 1, 9, 16, 1, tzinfo=UTC),
        correlation_id=correlation_id,
        session_id="sess-001",
        type="SyntheticOrderCreated",
        payload={
            "order_id": order_id,
            "reason": "fill_for_unknown_order",
        },
    )


def _kill_switch_tripped_event(
    reason: str = "risk_breach",
    correlation_id: str = "corr-kill",
) -> Event:
    return Event(
        event_id="evt-kill-switch",
        event_time=datetime(2026, 1, 1, 9, 18, tzinfo=UTC),
        processed_time=datetime(2026, 1, 1, 9, 18, 1, tzinfo=UTC),
        correlation_id=correlation_id,
        session_id="sess-001",
        type="KillSwitchTripped",
        payload={
            "reason": reason,
            "tripped_at": "2026-01-01T09:18:00+00:00",
        },
    )


# =============================================================================
# OrderBookProjector Tests
# =============================================================================

class TestOrderBookProjector:
    def test_apply_order_placed_registers_order(self):
        projector = OrderBookProjector()
        event = _order_placed_event()
        projector.apply(event)

        order = projector.get_order("ord-001")
        assert order is not None
        assert order.order_id == "ord-001"
        assert order.instrument == "NSE:RELIANCE"
        assert order.side == "BUY"
        assert order.quantity == Decimal("10")
        assert order.price == Decimal("2500")
        assert order.status == "ACK"
        assert order.filled_quantity == Decimal("0")
        assert order.correlation_id == "corr-001"

    def test_get_order_returns_none_for_unknown(self):
        projector = OrderBookProjector()
        assert projector.get_order("nonexistent") is None

    def test_get_all_orders_returns_all(self):
        projector = OrderBookProjector()
        projector.apply(_order_placed_event(order_id="ord-001"))
        projector.apply(_order_placed_event(order_id="ord-002", correlation_id="corr-002"))

        orders = projector.get_all_orders()
        assert len(orders) == 2
        ids = {o.order_id for o in orders}
        assert ids == {"ord-001", "ord-002"}

    def test_get_all_orders_empty_initially(self):
        projector = OrderBookProjector()
        assert projector.get_all_orders() == []

    def test_apply_order_filled_updates_filled_quantity(self):
        projector = OrderBookProjector()
        projector.apply(_order_placed_event())
        projector.apply(_order_filled_event(order_id="ord-001"))

        order = projector.get_order("ord-001")
        assert order is not None
        assert order.filled_quantity == Decimal("5")
        assert order.status == "PARTIALLY_FILLED"

    def test_apply_order_filled_complete_status(self):
        projector = OrderBookProjector()
        projector.apply(_order_placed_event())
        projector.apply(_order_filled_event(
            order_id="ord-001",
            cumulative_filled="10",
            is_complete=True,
        ))

        order = projector.get_order("ord-001")
        assert order is not None
        assert order.filled_quantity == Decimal("10")
        assert order.status == "FILLED"

    def test_apply_order_cancelled_sets_status(self):
        projector = OrderBookProjector()
        projector.apply(_order_placed_event())
        projector.apply(_order_cancelled_event(order_id="ord-001"))

        order = projector.get_order("ord-001")
        assert order is not None
        assert order.status == "CANCELLED"

    def test_apply_order_rejected_with_order_id(self):
        projector = OrderBookProjector()
        # Rejected events may carry an order_id if one was assigned before rejection
        projector.apply(_order_rejected_event(order_id="ord-rej"))

        order = projector.get_order("ord-rej")
        assert order is not None
        assert order.status == "REJECTED"

    def test_get_orders_by_status(self):
        projector = OrderBookProjector()
        projector.apply(_order_placed_event(order_id="ord-001"))
        projector.apply(_order_placed_event(order_id="ord-002", correlation_id="corr-002"))
        projector.apply(_order_filled_event(
            order_id="ord-001",
            cumulative_filled="10",
            is_complete=True,
        ))

        filled_orders = projector.get_orders_by_status("FILLED")
        assert len(filled_orders) == 1
        assert filled_orders[0].order_id == "ord-001"

        ack_orders = projector.get_orders_by_status("ACK")
        assert len(ack_orders) == 1
        assert ack_orders[0].order_id == "ord-002"

    def test_get_orders_by_status_no_match(self):
        projector = OrderBookProjector()
        projector.apply(_order_placed_event())
        assert projector.get_orders_by_status("FILLED") == []

    def test_apply_synthetic_order_created_tracks_order(self):
        projector = OrderBookProjector()
        projector.apply(_synthetic_order_created_event(order_id="ext-ord-001"))

        order = projector.get_order("ext-ord-001")
        assert order is not None
        assert order.order_id == "ext-ord-001"
        assert order.instrument == "unknown"
        assert order.side == "UNKNOWN"
        assert order.status == "ACK"

    def test_apply_kill_switch_tripped_tracks_state(self):
        projector = OrderBookProjector()
        projector.apply(_kill_switch_tripped_event())

        assert projector.is_kill_switch_tripped() is True

    def test_apply_kill_switch_no_order_impact(self):
        """KillSwitchTripped does not cancel orders in the projector — that's the actor's job."""
        projector = OrderBookProjector()
        projector.apply(_order_placed_event())
        projector.apply(_kill_switch_tripped_event())

        # Order status unchanged by kill switch in the projector
        order = projector.get_order("ord-001")
        assert order is not None
        assert order.status == "ACK"

    def test_unknown_event_type_ignored_gracefully(self):
        projector = OrderBookProjector()
        event = Event(
            event_id="evt-unknown",
            event_time=datetime(2026, 1, 1, 9, 20, tzinfo=UTC),
            processed_time=datetime(2026, 1, 1, 9, 20, 1, tzinfo=UTC),
            correlation_id="corr-x",
            session_id="sess-001",
            type="SomeUnknownEvent",
            payload={},
        )
        projector.apply(event)  # Should not raise

    def test_rebuild_from_events_produces_same_state(self):
        """Projector state can be fully rebuilt by replaying events."""
        events = [
            _order_placed_event(order_id="ord-001"),
            _order_placed_event(order_id="ord-002", correlation_id="corr-002"),
            _order_filled_event(order_id="ord-001", fill_quantity="5", cumulative_filled="5"),
            _order_filled_event(
                order_id="ord-001",
                fill_quantity="5",
                cumulative_filled="10",
                is_complete=True,
                fill_id="trade-002",
                correlation_id="corr-003",
            ),
            _order_cancelled_event(order_id="ord-002"),
        ]

        # First projector applies events incrementally
        p1 = OrderBookProjector()
        for e in events:
            p1.apply(e)

        # Second projector rebuilt from scratch
        p2 = OrderBookProjector()
        for e in events:
            p2.apply(e)

        assert len(p1.get_all_orders()) == len(p2.get_all_orders())
        assert p1.get_order("ord-001") == p2.get_order("ord-001")
        assert p1.get_order("ord-002") == p2.get_order("ord-002")

    def test_order_view_is_dataclass(self):
        """OrderView must be a frozen/immutable dataclass."""
        view = OrderView(
            order_id="x",
            instrument="NSE:RELIANCE",
            side="BUY",
            quantity=Decimal("1"),
            price=Decimal("100"),
            status="ACK",
            filled_quantity=Decimal("0"),
            correlation_id="c",
        )
        assert view.order_id == "x"
        # Frozen — mutation should fail
        with pytest.raises(AttributeError):
            view.status = "FILLED"


# =============================================================================
# PositionProjector Tests
# =============================================================================

class TestPositionProjector:
    def test_apply_position_updated_registers_position(self):
        projector = PositionProjector()
        projector.apply(_position_updated_event())

        pos = projector.get_position("NSE:RELIANCE")
        assert pos is not None
        assert pos.instrument == "NSE:RELIANCE"
        assert pos.quantity == Decimal("5")
        assert pos.avg_price == Decimal("2500")
        assert pos.realized_pnl == Decimal("0")

    def test_get_position_returns_none_for_unknown(self):
        projector = PositionProjector()
        assert projector.get_position("NSE:UNKNOWN") is None

    def test_get_all_positions(self):
        projector = PositionProjector()
        projector.apply(_position_updated_event(instrument="NSE:RELIANCE"))
        projector.apply(_position_updated_event(
            instrument="NSE:TCS",
            net_quantity="3",
            avg_price="3500",
            correlation_id="corr-003",
        ))

        positions = projector.get_all_positions()
        assert len(positions) == 2
        instruments = {p.instrument for p in positions}
        assert instruments == {"NSE:RELIANCE", "NSE:TCS"}

    def test_get_all_positions_empty_initially(self):
        projector = PositionProjector()
        assert projector.get_all_positions() == []

    def test_position_updated_overwrites_previous(self):
        projector = PositionProjector()
        projector.apply(_position_updated_event(net_quantity="5"))
        projector.apply(
            _position_updated_event(
                net_quantity="8",
                avg_price="2505",
                realized_pnl="10",
                correlation_id="corr-003",
            )
        )

        pos = projector.get_position("NSE:RELIANCE")
        assert pos is not None
        assert pos.quantity == Decimal("8")
        assert pos.avg_price == Decimal("2505")
        assert pos.realized_pnl == Decimal("10")

    def test_position_view_is_dataclass(self):
        """PositionView must be a frozen/immutable dataclass."""
        view = PositionView(
            instrument="NSE:RELIANCE",
            quantity=Decimal("5"),
            avg_price=Decimal("2500"),
            realized_pnl=Decimal("0"),
        )
        assert view.instrument == "NSE:RELIANCE"
        # Frozen — mutation should fail
        with pytest.raises(AttributeError):
            view.quantity = Decimal("10")


# =============================================================================
# Cross-projector rebuild test (from brief)
# =============================================================================

def test_projectors_can_be_rebuilt_from_events():
    """Apply events to one projector, create new projector, replay events, verify same state."""
    events = [
        _order_placed_event(order_id="ord-001"),
        _order_filled_event(
            order_id="ord-001",
            fill_quantity="10",
            cumulative_filled="10",
            is_complete=True,
        ),
        _position_updated_event(net_quantity="10"),
    ]

    order_p1 = OrderBookProjector()
    position_p1 = PositionProjector()
    for e in events:
        order_p1.apply(e)
        position_p1.apply(e)

    # Rebuild fresh projectors
    order_p2 = OrderBookProjector()
    position_p2 = PositionProjector()
    for e in events:
        order_p2.apply(e)
        position_p2.apply(e)

    assert order_p1.get_order("ord-001") == order_p2.get_order("ord-001")
    assert position_p1.get_position("NSE:RELIANCE") == position_p2.get_position("NSE:RELIANCE")


def test_projectors_handle_synthetic_orders():
    """Apply SyntheticOrderCreated, verify order is tracked."""
    projector = OrderBookProjector()
    projector.apply(_synthetic_order_created_event(order_id="synth-001"))

    order = projector.get_order("synth-001")
    assert order is not None
    assert order.order_id == "synth-001"
    assert order.instrument == "unknown"
    assert order.side == "UNKNOWN"
