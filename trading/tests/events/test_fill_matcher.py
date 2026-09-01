"""Tests for FillMatcher — translates broker order-stream updates into ApplyFillCommands."""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradex_domain import Equity, OrderRequest, OrderSide, OrderType, Price, Quantity
from tradex_trading.events.actor import (
    ApplyFillCommand,
    OrderBookActor,
    PlaceOrderCommand,
)
from tradex_trading.events.fill_matcher import BrokerOrderUpdate, FillMatcher
from tradex_trading.events.processor import CommandProcessor
from tradex_trading.events.store import EventStore


@pytest.fixture
def setup():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    store = EventStore(path)
    actor = OrderBookActor(session_id="test-sess", event_store=store)
    processor = CommandProcessor(event_store=store, order_book=actor)
    matcher = FillMatcher(command_processor=processor)
    yield store, actor, processor, matcher
    store.close()
    os.unlink(path)


@pytest.fixture
def sample_request():
    return OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(Decimal("10")),
        price=Price(Decimal("2500")),
        correlation_id="corr-001",
    )


def test_register_and_process_fill(setup, sample_request):
    """Register order, process fill update, verify ApplyFillCommand sent."""
    store, actor, processor, matcher = setup

    # Place an order through the processor
    place_cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    place_result = processor.process(place_cmd)
    order_id = place_result.events[0].payload["order_id"]

    # Register the order with the FillMatcher (mapping internal order_id to broker_order_id)
    matcher.register_order(
        order_id=order_id,
        broker_order_id="BROKER-ORD-123",
        instrument="NSE:RELIANCE",
        side="BUY",
    )

    # Process a fill update from the broker
    update = BrokerOrderUpdate(
        broker_order_id="BROKER-ORD-123",
        instrument="NSE:RELIANCE",
        side="BUY",
        quantity=10,
        filled_quantity=5,
        fill_price=Decimal("2500"),
        status="PARTIALLY_FILLED",
        timestamp=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    result = matcher.process_update(update)

    assert result is not None
    assert result.success is True
    # Verify the OrderFilled event was emitted
    filled_events = [e for e in result.events if e.type == "OrderFilled"]
    assert len(filled_events) == 1
    assert filled_events[0].payload["fill_quantity"] == "5"
    assert filled_events[0].payload["cumulative_filled"] == "5"


def test_duplicate_update_skipped(setup, sample_request):
    """Process same update twice, second time returns None."""
    store, actor, processor, matcher = setup

    place_cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    place_result = processor.process(place_cmd)
    order_id = place_result.events[0].payload["order_id"]

    matcher.register_order(
        order_id=order_id,
        broker_order_id="BROKER-ORD-456",
        instrument="NSE:RELIANCE",
        side="BUY",
    )

    update = BrokerOrderUpdate(
        broker_order_id="BROKER-ORD-456",
        instrument="NSE:RELIANCE",
        side="BUY",
        quantity=10,
        filled_quantity=5,
        fill_price=Decimal("2500"),
        status="PARTIALLY_FILLED",
        timestamp=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )

    # First update — should succeed
    result1 = matcher.process_update(update)
    assert result1 is not None
    assert result1.success is True

    # Second identical update — should be skipped (duplicate)
    result2 = matcher.process_update(update)
    assert result2 is None


def test_stale_update_skipped(setup, sample_request):
    """Process update with lower fill_quantity, verify skipped."""
    store, actor, processor, matcher = setup

    place_cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    place_result = processor.process(place_cmd)
    order_id = place_result.events[0].payload["order_id"]

    matcher.register_order(
        order_id=order_id,
        broker_order_id="BROKER-ORD-789",
        instrument="NSE:RELIANCE",
        side="BUY",
    )

    # First update: filled 8
    update1 = BrokerOrderUpdate(
        broker_order_id="BROKER-ORD-789",
        instrument="NSE:RELIANCE",
        side="BUY",
        quantity=10,
        filled_quantity=8,
        fill_price=Decimal("2500"),
        status="PARTIALLY_FILLED",
        timestamp=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    result1 = matcher.process_update(update1)
    assert result1 is not None
    assert result1.success is True

    # Stale update: filled 5 (less than 8) — should be skipped
    update2 = BrokerOrderUpdate(
        broker_order_id="BROKER-ORD-789",
        instrument="NSE:RELIANCE",
        side="BUY",
        quantity=10,
        filled_quantity=5,
        fill_price=Decimal("2500"),
        status="PARTIALLY_FILLED",
        timestamp=datetime(2026, 1, 1, 9, 17, tzinfo=UTC),
    )
    result2 = matcher.process_update(update2)
    assert result2 is None


def test_unknown_broker_order_returns_none(setup):
    """Process update for unregistered broker_order_id."""
    store, actor, processor, matcher = setup

    update = BrokerOrderUpdate(
        broker_order_id="UNKNOWN-BROKER-ORD",
        instrument="NSE:RELIANCE",
        side="BUY",
        quantity=10,
        filled_quantity=5,
        fill_price=Decimal("2500"),
        status="PARTIALLY_FILLED",
        timestamp=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    result = matcher.process_update(update)
    assert result is None


def test_partial_fill_then_full_fill(setup, sample_request):
    """Process partial fill, then full fill, verify both commands sent."""
    store, actor, processor, matcher = setup

    place_cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    place_result = processor.process(place_cmd)
    order_id = place_result.events[0].payload["order_id"]

    matcher.register_order(
        order_id=order_id,
        broker_order_id="BROKER-ORD-FULL",
        instrument="NSE:RELIANCE",
        side="BUY",
    )

    # Partial fill: 3 out of 10
    partial_update = BrokerOrderUpdate(
        broker_order_id="BROKER-ORD-FULL",
        instrument="NSE:RELIANCE",
        side="BUY",
        quantity=10,
        filled_quantity=3,
        fill_price=Decimal("2500"),
        status="PARTIALLY_FILLED",
        timestamp=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    result1 = matcher.process_update(partial_update)
    assert result1 is not None
    assert result1.success is True
    filled1 = [e for e in result1.events if e.type == "OrderFilled"]
    assert len(filled1) == 1
    assert filled1[0].payload["fill_quantity"] == "3"
    assert filled1[0].payload["is_complete"] is False

    # Full fill: 10 out of 10
    full_update = BrokerOrderUpdate(
        broker_order_id="BROKER-ORD-FULL",
        instrument="NSE:RELIANCE",
        side="BUY",
        quantity=10,
        filled_quantity=10,
        fill_price=Decimal("2501"),
        status="FILLED",
        timestamp=datetime(2026, 1, 1, 9, 17, tzinfo=UTC),
    )
    result2 = matcher.process_update(full_update)
    assert result2 is not None
    assert result2.success is True
    filled2 = [e for e in result2.events if e.type == "OrderFilled"]
    assert len(filled2) == 1
    assert filled2[0].payload["fill_quantity"] == "7"  # 10 - 3
    assert filled2[0].payload["cumulative_filled"] == "10"
    assert filled2[0].payload["is_complete"] is True


def test_unregister_order_removes_mapping(setup, sample_request):
    """Unregister order, verify subsequent updates return None."""
    store, actor, processor, matcher = setup

    place_cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    place_result = processor.process(place_cmd)
    order_id = place_result.events[0].payload["order_id"]

    matcher.register_order(
        order_id=order_id,
        broker_order_id="BROKER-ORD-UNREG",
        instrument="NSE:RELIANCE",
        side="BUY",
    )

    # Unregister
    matcher.unregister_order(order_id)

    update = BrokerOrderUpdate(
        broker_order_id="BROKER-ORD-UNREG",
        instrument="NSE:RELIANCE",
        side="BUY",
        quantity=10,
        filled_quantity=5,
        fill_price=Decimal("2500"),
        status="PARTIALLY_FILLED",
        timestamp=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    result = matcher.process_update(update)
    assert result is None


def test_fill_matcher_tracks_last_filled_quantity(setup, sample_request):
    """Verify FillMatcher tracks last processed fill_quantity per broker order."""
    store, actor, processor, matcher = setup

    place_cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    place_result = processor.process(place_cmd)
    order_id = place_result.events[0].payload["order_id"]

    matcher.register_order(
        order_id=order_id,
        broker_order_id="BROKER-ORD-TRACK",
        instrument="NSE:RELIANCE",
        side="BUY",
    )

    # Process incremental fills
    for filled, price in [(2, Decimal("2500")), (5, Decimal("2501")), (10, Decimal("2502"))]:
        update = BrokerOrderUpdate(
            broker_order_id="BROKER-ORD-TRACK",
            instrument="NSE:RELIANCE",
            side="BUY",
            quantity=10,
            filled_quantity=filled,
            fill_price=price,
            status="PARTIALLY_FILLED" if filled < 10 else "FILLED",
            timestamp=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
        )
        result = matcher.process_update(update)
        assert result is not None
        assert result.success is True

    # Verify final state: order should be fully filled
    assert actor._orders[order_id].filled_quantity == Decimal("10")
    assert actor._orders[order_id].status.value == "FILLED"


def test_multiple_orders_independent_tracking(setup, sample_request):
    """Multiple orders tracked independently — fills don't interfere."""
    store, actor, processor, matcher = setup

    # Place two orders
    place1 = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    r1 = processor.process(place1)
    oid1 = r1.events[0].payload["order_id"]

    place2 = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-002",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    r2 = processor.process(place2)
    oid2 = r2.events[0].payload["order_id"]

    matcher.register_order(oid1, "BROKER-A", "NSE:RELIANCE", "BUY")
    matcher.register_order(oid2, "BROKER-B", "NSE:RELIANCE", "BUY")

    # Fill order 1 partially
    update1 = BrokerOrderUpdate(
        broker_order_id="BROKER-A",
        instrument="NSE:RELIANCE",
        side="BUY",
        quantity=10,
        filled_quantity=3,
        fill_price=Decimal("2500"),
        status="PARTIALLY_FILLED",
        timestamp=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    matcher.process_update(update1)

    # Fill order 2 fully
    update2 = BrokerOrderUpdate(
        broker_order_id="BROKER-B",
        instrument="NSE:RELIANCE",
        side="BUY",
        quantity=10,
        filled_quantity=10,
        fill_price=Decimal("2501"),
        status="FILLED",
        timestamp=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    matcher.process_update(update2)

    # Verify independent state
    assert actor._orders[oid1].filled_quantity == Decimal("3")
    assert actor._orders[oid2].filled_quantity == Decimal("10")


def test_retry_after_rejection_tracker_not_advanced(setup, sample_request):
    """When the actor returns empty events (rejection/no-op), the tracker
    must NOT advance — this allows the next update to retry the fill.

    Scenario:
    1. Order placed, registered with FillMatcher (tracker at 0)
    2. Actor's state diverges — its filled_quantity is already 5
    3. FillMatcher processes fill of 5 — sends to processor
    4. Actor computes delta=0, returns empty events (no-op)
    5. Tracker must stay at 0 (NOT advanced)
    6. Actor's state corrected (filled back to 0)
    7. FillMatcher processes fill of 5 again — sends to processor
    8. Actor computes delta=5, emits events
    9. Tracker advances to 5
    """
    store, actor, processor, matcher = setup

    # Place an order through the processor
    place_cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    place_result = processor.process(place_cmd)
    order_id = place_result.events[0].payload["order_id"]

    # Register the order with the FillMatcher
    matcher.register_order(
        order_id=order_id,
        broker_order_id="BROKER-ORD-RETRY",
        instrument="NSE:RELIANCE",
        side="BUY",
    )

    # Simulate state divergence: actor's filled_quantity is already 5
    # (e.g., due to recovery desync or partial replay)
    actor._orders[order_id].filled_quantity = Decimal("5")

    # First update: filled_quantity=5 — FillMatcher sends to processor,
    # but actor returns empty events (delta=0, no-op)
    update = BrokerOrderUpdate(
        broker_order_id="BROKER-ORD-RETRY",
        instrument="NSE:RELIANCE",
        side="BUY",
        quantity=10,
        filled_quantity=5,
        fill_price=Decimal("2500"),
        status="PARTIALLY_FILLED",
        timestamp=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    result = matcher.process_update(update)

    # Actor returned empty events — result.success=True but no events
    assert result is not None
    assert result.success is True
    assert len(result.events) == 0

    # CRITICAL: Tracker must NOT have advanced — still at 0
    assert matcher._last_filled["BROKER-ORD-RETRY"] == 0

    # Correct the actor's state (simulating recovery/correction)
    actor._orders[order_id].filled_quantity = Decimal("0")

    # Retry: same fill_quantity=5 — this time actor accepts (delta=5)
    result2 = matcher.process_update(update)

    # Actor now emits events
    assert result2 is not None
    assert result2.success is True
    assert len(result2.events) > 0
    filled_events = [e for e in result2.events if e.type == "OrderFilled"]
    assert len(filled_events) == 1
    assert filled_events[0].payload["fill_quantity"] == "5"
    assert filled_events[0].payload["cumulative_filled"] == "5"

    # Tracker now advanced to 5
    assert matcher._last_filled["BROKER-ORD-RETRY"] == 5
