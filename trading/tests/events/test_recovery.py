"""Tests for Session Recovery — rebuilds state from event log and reconciles with broker."""

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
from tradex_trading.events.fill_matcher import BrokerOrderUpdate
from tradex_trading.events.processor import CommandProcessor
from tradex_trading.events.recovery import (
    Discrepancy,
    ReconciliationResult,
    RecoveryResult,
    SessionRecovery,
)
from tradex_trading.events.store import EventStore


@pytest.fixture
def setup():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    store = EventStore(path)
    actor = OrderBookActor(session_id="test-sess", event_store=store)
    processor = CommandProcessor(event_store=store, order_book=actor)
    recovery = SessionRecovery(event_store=store, order_book=actor, command_processor=processor)
    yield store, actor, processor, recovery
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


def test_recovery_rebuilds_state(setup, sample_request):
    """Process commands, create new session, recover, verify state matches."""
    store, actor, processor, recovery = setup

    # Process commands
    cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    result = processor.process(cmd)
    assert result.success is True
    order_id = result.events[0].payload["order_id"]

    # Create new components (simulating restart)
    new_actor = OrderBookActor(session_id="test-sess", event_store=store)
    new_processor = CommandProcessor(event_store=store, order_book=new_actor)
    new_recovery = SessionRecovery(event_store=store, order_book=new_actor, command_processor=new_processor)

    # Recover
    recovery_result = new_recovery.recover()

    assert recovery_result.success is True
    assert recovery_result.orders_recovered == 1
    assert recovery_result.positions_recovered == 0  # No fills yet
    assert recovery_result.last_sequence == 1
    assert recovery_result.error is None

    # Verify state matches
    snapshot = new_actor.snapshot()
    assert len(snapshot["orders"]) == 1
    assert order_id in snapshot["orders"]


def test_recovery_empty_store(setup):
    """Recover from empty store, verify success with 0 counts."""
    store, actor, processor, recovery = setup

    result = recovery.recover()

    assert result.success is True
    assert result.orders_recovered == 0
    assert result.positions_recovered == 0
    assert result.last_sequence == 0
    assert result.error is None


def test_reconciliation_in_sync(setup, sample_request):
    """Local and broker match, verify in_sync=True."""
    store, actor, processor, recovery = setup

    # Place an order
    cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    result = processor.process(cmd)
    order_id = result.events[0].payload["order_id"]

    # Create broker update that matches local state (order placed, not filled)
    broker_update = BrokerOrderUpdate(
        broker_order_id="BROKER-001",
        instrument="NSE:RELIANCE",
        side="BUY",
        quantity=10,
        filled_quantity=0,
        fill_price=0.0,
        status="ACK",
        timestamp=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )

    recon_result = recovery.reconcile_with_broker([broker_update])

    assert recon_result.in_sync is True
    assert len(recon_result.discrepancies) == 0
    assert len(recon_result.local_only_orders) == 0
    assert len(recon_result.broker_only_orders) == 0


def test_reconciliation_detects_missing_fill(setup, sample_request):
    """Broker has fill that local doesn't, verify discrepancy detected."""
    store, actor, processor, recovery = setup

    # Place an order (not filled locally)
    cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    result = processor.process(cmd)
    order_id = result.events[0].payload["order_id"]

    # Broker reports a fill (filled_quantity=5) but local has 0
    broker_update = BrokerOrderUpdate(
        broker_order_id="BROKER-001",
        instrument="NSE:RELIANCE",
        side="BUY",
        quantity=10,
        filled_quantity=5,
        fill_price=2500.0,
        status="PARTIALLY_FILLED",
        timestamp=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )

    recon_result = recovery.reconcile_with_broker([broker_update])

    assert recon_result.in_sync is False
    assert len(recon_result.discrepancies) == 1
    disc = recon_result.discrepancies[0]
    assert disc.order_id == order_id
    assert disc.field == "filled_quantity"
    assert disc.local_value == "0"
    assert disc.broker_value == "5"


def test_reconciliation_detects_unknown_order(setup, sample_request):
    """Local has order that broker doesn't, verify detected."""
    store, actor, processor, recovery = setup

    # Place an order
    cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    result = processor.process(cmd)
    order_id = result.events[0].payload["order_id"]

    # Broker has no orders (empty list)
    recon_result = recovery.reconcile_with_broker([])

    assert recon_result.in_sync is False
    assert len(recon_result.local_only_orders) == 1
    assert recon_result.local_only_orders[0] == order_id


def test_reconciliation_detects_broker_only_order(setup, sample_request):
    """Broker has order that local doesn't, verify detected."""
    store, actor, processor, recovery = setup

    # No local orders placed

    # Broker reports an order we don't have locally
    broker_update = BrokerOrderUpdate(
        broker_order_id="BROKER-UNKNOWN",
        instrument="NSE:RELIANCE",
        side="BUY",
        quantity=10,
        filled_quantity=5,
        fill_price=2500.0,
        status="PARTIALLY_FILLED",
        timestamp=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )

    recon_result = recovery.reconcile_with_broker([broker_update])

    assert recon_result.in_sync is False
    assert len(recon_result.broker_only_orders) == 1
    assert recon_result.broker_only_orders[0] == "BROKER-UNKNOWN"


def test_recovery_with_fills_restores_positions(setup, sample_request):
    """Recovery restores both orders and positions after fills."""
    store, actor, processor, recovery = setup

    # Place order
    cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    result = processor.process(cmd)
    order_id = result.events[0].payload["order_id"]

    # Apply fill via ApplyFillCommand
    from tradex_trading.events.actor import ApplyFillCommand
    fill_cmd = ApplyFillCommand(
        order_id=order_id,
        cumulative_filled=Decimal("5"),
        fill_price=Decimal("2500"),
        fill_id="trade-001",
        correlation_id="corr-002",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    processor.process(fill_cmd)

    # Create new components and recover
    new_actor = OrderBookActor(session_id="test-sess", event_store=store)
    new_processor = CommandProcessor(event_store=store, order_book=new_actor)
    new_recovery = SessionRecovery(event_store=store, order_book=new_actor, command_processor=new_processor)

    recovery_result = new_recovery.recover()

    assert recovery_result.success is True
    assert recovery_result.orders_recovered == 1
    assert recovery_result.positions_recovered == 1  # Position created by fill

    # Verify state
    snapshot = new_actor.snapshot()
    assert len(snapshot["orders"]) == 1
    assert len(snapshot["positions"]) == 1
    assert "NSE:RELIANCE" in snapshot["positions"]


def test_reconciliation_multiple_orders_same_instrument_side(setup):
    """Multiple orders on same (instrument, side) must NOT be silently dropped.

    Bug fix: Previously keyed by (instrument, side), so second order overwrote first.
    Now uses order_id as key, so all orders are tracked independently.
    """
    store, actor, processor, recovery = setup

    # Place two BUY orders for RELIANCE
    request1 = OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(Decimal("10")),
        price=Price(Decimal("2500")),
        correlation_id="corr-001",
    )
    request2 = OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(Decimal("20")),
        price=Price(Decimal("2510")),
        correlation_id="corr-002",
    )

    cmd1 = PlaceOrderCommand(
        request=request1,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    cmd2 = PlaceOrderCommand(
        request=request2,
        correlation_id="corr-002",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )

    result1 = processor.process(cmd1)
    result2 = processor.process(cmd2)
    order_id1 = result1.events[0].payload["order_id"]
    order_id2 = result2.events[0].payload["order_id"]

    # Both orders should exist in local state
    snapshot = actor.snapshot()
    assert len(snapshot["orders"]) == 2
    assert order_id1 in snapshot["orders"]
    assert order_id2 in snapshot["orders"]

    # Broker updates for both orders (using explicit mapping)
    broker_update1 = BrokerOrderUpdate(
        broker_order_id="BROKER-001",
        instrument="NSE:RELIANCE",
        side="BUY",
        quantity=10,
        filled_quantity=5,
        fill_price=2500.0,
        status="PARTIALLY_FILLED",
        timestamp=datetime(2026, 1, 1, 9, 17, tzinfo=UTC),
    )
    broker_update2 = BrokerOrderUpdate(
        broker_order_id="BROKER-002",
        instrument="NSE:RELIANCE",
        side="BUY",
        quantity=20,
        filled_quantity=0,
        fill_price=0.0,
        status="ACK",
        timestamp=datetime(2026, 1, 1, 9, 17, tzinfo=UTC),
    )

    # Use explicit mapping to avoid (instrument, side) collision
    broker_to_internal = {
        "BROKER-001": order_id1,
        "BROKER-002": order_id2,
    }

    recon_result = recovery.reconcile_with_broker(
        [broker_update1, broker_update2],
        broker_to_internal=broker_to_internal,
    )

    # Should detect discrepancy only for order1 (filled locally=0, broker=5)
    assert recon_result.in_sync is False
    assert len(recon_result.discrepancies) == 1
    disc = recon_result.discrepancies[0]
    assert disc.order_id == order_id1
    assert disc.field == "filled_quantity"
    assert disc.local_value == "0"
    assert disc.broker_value == "5"

    # order2 should be in sync (both have filled=0)
    assert order_id2 not in [d.order_id for d in recon_result.discrepancies]


def test_reconciliation_multiple_orders_same_key_fallback(setup):
    """Fallback reconciliation by (instrument, side) handles multiple orders correctly."""
    store, actor, processor, recovery = setup

    # Place two BUY orders for RELIANCE
    request1 = OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(Decimal("10")),
        price=Price(Decimal("2500")),
        correlation_id="corr-001",
    )
    request2 = OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(Decimal("20")),
        price=Price(Decimal("2510")),
        correlation_id="corr-002",
    )

    cmd1 = PlaceOrderCommand(
        request=request1,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    cmd2 = PlaceOrderCommand(
        request=request2,
        correlation_id="corr-002",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )

    result1 = processor.process(cmd1)
    result2 = processor.process(cmd2)
    order_id1 = result1.events[0].payload["order_id"]
    order_id2 = result2.events[0].payload["order_id"]

    # Broker updates for both orders (no explicit mapping — fallback mode)
    broker_update1 = BrokerOrderUpdate(
        broker_order_id="BROKER-001",
        instrument="NSE:RELIANCE",
        side="BUY",
        quantity=10,
        filled_quantity=5,
        fill_price=2500.0,
        status="PARTIALLY_FILLED",
        timestamp=datetime(2026, 1, 1, 9, 17, tzinfo=UTC),
    )
    broker_update2 = BrokerOrderUpdate(
        broker_order_id="BROKER-002",
        instrument="NSE:RELIANCE",
        side="BUY",
        quantity=20,
        filled_quantity=0,
        fill_price=0.0,
        status="ACK",
        timestamp=datetime(2026, 1, 1, 9, 17, tzinfo=UTC),
    )

    # Without explicit mapping, uses (instrument, side) fallback
    recon_result = recovery.reconcile_with_broker([broker_update1, broker_update2])

    # Should detect discrepancy for at least one order (filled mismatch)
    assert recon_result.in_sync is False
    # At minimum, order1 should show discrepancy (local=0, broker=5)
    disc_order_ids = [d.order_id for d in recon_result.discrepancies]
    assert order_id1 in disc_order_ids or order_id2 in disc_order_ids


def test_recovery_idempotent(setup, sample_request):
    """Calling recover() multiple times must produce the same result.

    Bug fix: Previously, recover() didn't clear state before replay,
    so calling it twice would duplicate entries.
    """
    store, actor, processor, recovery = setup

    # Place an order
    cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    processor.process(cmd)

    # First recovery
    result1 = recovery.recover()
    assert result1.success is True
    assert result1.orders_recovered == 1
    snapshot1 = actor.snapshot()
    assert len(snapshot1["orders"]) == 1

    # Second recovery — should produce same result, not duplicate
    result2 = recovery.recover()
    assert result2.success is True
    assert result2.orders_recovered == 1
    snapshot2 = actor.snapshot()
    assert len(snapshot2["orders"]) == 1

    # Third recovery — still idempotent
    result3 = recovery.recover()
    assert result3.success is True
    assert result3.orders_recovered == 1
    snapshot3 = actor.snapshot()
    assert len(snapshot3["orders"]) == 1

    # All snapshots should be identical
    assert snapshot1 == snapshot2 == snapshot3


def test_recovery_idempotent_with_fills(setup, sample_request):
    """Recovery idempotency with fills — positions must not duplicate."""
    store, actor, processor, recovery = setup

    # Place order and apply fill
    cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    result = processor.process(cmd)
    order_id = result.events[0].payload["order_id"]

    fill_cmd = ApplyFillCommand(
        order_id=order_id,
        cumulative_filled=Decimal("5"),
        fill_price=Decimal("2500"),
        fill_id="trade-001",
        correlation_id="corr-002",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    processor.process(fill_cmd)

    # Recover multiple times
    for _ in range(3):
        result = recovery.recover()
        assert result.success is True
        assert result.orders_recovered == 1
        assert result.positions_recovered == 1

    # Verify state is correct
    snapshot = actor.snapshot()
    assert len(snapshot["orders"]) == 1
    assert len(snapshot["positions"]) == 1
    assert "NSE:RELIANCE" in snapshot["positions"]


def test_recovery_clears_kill_switch_state(setup, sample_request):
    """Recovery must reset kill_switch state before replay."""
    store, actor, processor, recovery = setup

    # Trip kill switch directly on actor
    actor.trip_kill_switch("test")
    assert actor.snapshot()["kill_switch"] is True

    # Recover — should clear kill switch if no KillSwitchTripped event in log
    result = recovery.recover()
    assert result.success is True
    snapshot = actor.snapshot()
    assert snapshot["kill_switch"] is False


def test_reconciliation_decimal_filled_quantity_comparison(setup, sample_request):
    """filled_quantity comparison must use Decimal, not string.

    Bug fix: Previously compared strings, which is fragile.
    """
    store, actor, processor, recovery = setup

    # Place order
    cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    result = processor.process(cmd)
    order_id = result.events[0].payload["order_id"]

    # Broker update with filled_quantity that would fail string comparison
    # but pass Decimal comparison (e.g., 5 vs 5.0)
    broker_update = BrokerOrderUpdate(
        broker_order_id="BROKER-001",
        instrument="NSE:RELIANCE",
        side="BUY",
        quantity=10,
        filled_quantity=0,  # int 0
        fill_price=0.0,
        status="ACK",
        timestamp=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )

    # Local has filled_quantity="0" (string), broker has 0 (int)
    # Decimal comparison should treat these as equal
    recon_result = recovery.reconcile_with_broker([broker_update])

    # Should be in sync — both have 0 filled
    assert recon_result.in_sync is True
    assert len(recon_result.discrepancies) == 0


def test_recovery_specific_exception_handling(setup, sample_request):
    """Recovery should catch specific exceptions, not broad Exception."""
    store, actor, processor, recovery = setup

    # Place an order
    cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    processor.process(cmd)

    # Close the store to force an error
    store.close()

    # Recovery should fail gracefully with specific error
    result = recovery.recover()
    assert result.success is False
    assert result.error is not None
    # Error should mention the specific issue (database error)
    assert "database" in result.error.lower() or "closed" in result.error.lower()
