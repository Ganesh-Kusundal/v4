"""Tests for Session Recovery — rebuilds state from event log and reconciles with broker."""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradex_domain import Equity, OrderRequest, OrderSide, OrderType, Price, Quantity
from tradex_trading.events.actor import (
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
