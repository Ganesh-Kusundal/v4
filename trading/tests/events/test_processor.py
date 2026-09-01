"""Tests for Command Processor with Idempotency."""

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
from tradex_trading.events.processor import CommandProcessor, CommandResult
from tradex_trading.events.store import EventStore


@pytest.fixture
def setup():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    store = EventStore(path)
    actor = OrderBookActor(session_id="test-sess", event_store=store)
    processor = CommandProcessor(event_store=store, order_book=actor)
    yield store, actor, processor
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


def test_process_place_order_returns_events(setup, sample_request):
    """Process PlaceOrderCommand, verify CommandResult has events."""
    store, actor, processor = setup
    cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    result = processor.process(cmd)

    assert result.success is True
    assert len(result.events) >= 1
    assert result.events[0].type == "OrderPlaced"
    assert result.correlation_id == "corr-001"
    assert result.is_duplicate is False
    assert result.error is None


def test_process_duplicate_returns_cached(setup, sample_request):
    """Process same command twice, second time returns cached events."""
    store, actor, processor = setup
    cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    result1 = processor.process(cmd)
    result2 = processor.process(cmd)

    assert result2.is_duplicate is True
    assert result2.success is True
    assert len(result2.events) == len(result1.events)
    assert result2.events[0].event_id == result1.events[0].event_id


def test_process_releases_idempotency_on_rejection(setup, sample_request):
    """Process command that fails, verify correlation_id can be reused."""
    store, actor, processor = setup

    # Trip kill switch
    actor.trip_kill_switch("test")

    cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    result1 = processor.process(cmd)
    assert result1.success is False
    assert result1.is_duplicate is False
    assert result1.events[0].type == "OrderRejected"

    # Process the same command again - should NOT be detected as duplicate
    result2 = processor.process(cmd)
    assert result2.success is False  # Still rejected because kill switch is active
    assert result2.is_duplicate is False  # NOT a duplicate - it was re-processed


def test_recover_rebuilds_idempotency_map(setup, sample_request):
    """Process commands, create new processor, recover, verify duplicates detected."""
    store, actor, processor = setup

    cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    result1 = processor.process(cmd)
    assert result1.success is True
    assert result1.is_duplicate is False

    # Create a new processor with a fresh actor (same session_id)
    new_actor = OrderBookActor(session_id="test-sess", event_store=store)
    new_processor = CommandProcessor(event_store=store, order_book=new_actor)
    new_processor.recover()

    # Now processing the same command should return cached result
    result2 = new_processor.process(cmd)
    assert result2.is_duplicate is True
    assert result2.success is True
    assert result2.events[0].event_id == result1.events[0].event_id
