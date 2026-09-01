"""Tests for Order Book Actor — single writer, no locks, event-sourced."""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradex_domain import Equity, OrderRequest, OrderSide, OrderType, Price, Quantity
from tradex_trading.events.actor import (
    ApplyFillCommand,
    CancelOrderCommand,
    OrderBookActor,
    PlaceOrderCommand,
    TripKillSwitchCommand,
)
from tradex_trading.events.order_fsm import OrderState
from tradex_trading.events.store import EventStore


@pytest.fixture
def actor():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    store = EventStore(path)
    actor = OrderBookActor(session_id="test-sess", event_store=store)
    yield actor
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


def test_place_order_creates_order_and_events(actor, sample_request):
    # Verify OrderPlaced event is emitted with correct payload
    cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    events = actor.handle(cmd)

    assert len(events) >= 1
    placed = [e for e in events if e.type == "OrderPlaced"]
    assert len(placed) == 1
    assert placed[0].payload["instrument"] == "NSE:RELIANCE"
    assert placed[0].payload["side"] == "BUY"
    assert placed[0].payload["quantity"] == "10"


def test_place_order_writes_to_store(actor, sample_request):
    # Verify events are persisted to the store
    cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    events = actor.handle(cmd)

    stored = actor._store.read_all("test-sess")
    assert len(stored) == len(events)


def test_apply_fill_updates_order_and_position(actor, sample_request):
    # Place order, apply fill, verify OrderFilled + PositionUpdated events
    place_cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    place_events = actor.handle(place_cmd)
    order_id = place_events[0].payload["order_id"]

    # Then apply a fill
    fill_cmd = ApplyFillCommand(
        order_id=order_id,
        cumulative_filled=Decimal("5"),
        fill_price=Decimal("2500"),
        fill_id="trade-001",
        correlation_id="corr-002",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    fill_events = actor.handle(fill_cmd)

    filled = [e for e in fill_events if e.type == "OrderFilled"]
    assert len(filled) == 1
    assert filled[0].payload["fill_quantity"] == "5"
    assert filled[0].payload["cumulative_filled"] == "5"

    position = [e for e in fill_events if e.type == "PositionUpdated"]
    assert len(position) == 1
    assert position[0].payload["net_quantity"] == "5"


def test_duplicate_fill_is_idempotent(actor, sample_request):
    # Place order
    place_cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    place_events = actor.handle(place_cmd)
    order_id = place_events[0].payload["order_id"]

    # First fill
    fill_cmd = ApplyFillCommand(
        order_id=order_id,
        cumulative_filled=Decimal("5"),
        fill_price=Decimal("2500"),
        fill_id="trade-001",
        correlation_id="corr-002",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    actor.handle(fill_cmd)

    # Same fill again (duplicate) — should produce no events
    dup_cmd = ApplyFillCommand(
        order_id=order_id,
        cumulative_filled=Decimal("5"),
        fill_price=Decimal("2500"),
        fill_id="trade-001",
        correlation_id="corr-003",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    dup_events = actor.handle(dup_cmd)
    assert len(dup_events) == 0  # No new events for duplicate


def test_full_fill_transitions_to_filled(actor, sample_request):
    # Place order
    place_cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    place_events = actor.handle(place_cmd)
    order_id = place_events[0].payload["order_id"]

    # Full fill
    fill_cmd = ApplyFillCommand(
        order_id=order_id,
        cumulative_filled=Decimal("10"),
        fill_price=Decimal("2500"),
        fill_id="trade-001",
        correlation_id="corr-002",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    fill_events = actor.handle(fill_cmd)

    filled = [e for e in fill_events if e.type == "OrderFilled"]
    assert filled[0].payload["is_complete"] is True


def test_unknown_order_creates_synthetic(actor):
    fill_cmd = ApplyFillCommand(
        order_id="unknown-ord",
        cumulative_filled=Decimal("5"),
        fill_price=Decimal("2500"),
        fill_id="trade-001",
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    events = actor.handle(fill_cmd)

    synthetic = [e for e in events if e.type == "SyntheticOrderCreated"]
    assert len(synthetic) == 1
    assert synthetic[0].payload["reason"] == "fill_for_unknown_order"


def test_kill_switch_rejects_orders(actor, sample_request):
    # Trip kill switch
    actor.trip_kill_switch("test")

    cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    events = actor.handle(cmd)

    rejected = [e for e in events if e.type == "OrderRejected"]
    assert len(rejected) == 1
    assert "kill_switch" in rejected[0].payload["reason"]


def test_recovery_from_event_log(actor, sample_request):
    # Place order and fill
    place_cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    actor.handle(place_cmd)

    # Create a new actor with the same store (simulating recovery)
    new_actor = OrderBookActor(session_id="test-sess", event_store=actor._store)
    new_actor.recover()

    # State should be restored
    assert len(new_actor._orders) == 1


def test_cancel_order_transitions_to_cancelled(actor, sample_request):
    # Place order
    place_cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    place_events = actor.handle(place_cmd)
    order_id = place_events[0].payload["order_id"]

    # Cancel the order
    cancel_cmd = CancelOrderCommand(
        order_id=order_id,
        correlation_id="corr-002",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    cancel_events = actor.handle(cancel_cmd)

    cancelled = [e for e in cancel_events if e.type == "OrderCancelled"]
    assert len(cancelled) == 1

    # Verify state
    assert actor._orders[order_id].status == OrderState.CANCELLED


def test_cancel_unknown_order_returns_empty(actor):
    cancel_cmd = CancelOrderCommand(
        order_id="nonexistent",
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    events = actor.handle(cancel_cmd)
    assert len(events) == 0


def test_cancel_already_cancelled_order_returns_empty(actor, sample_request):
    # Place order
    place_cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    place_events = actor.handle(place_cmd)
    order_id = place_events[0].payload["order_id"]

    # Cancel once
    cancel_cmd = CancelOrderCommand(
        order_id=order_id,
        correlation_id="corr-002",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    actor.handle(cancel_cmd)

    # Cancel again — should be empty since already terminal
    dup_cancel = CancelOrderCommand(
        order_id=order_id,
        correlation_id="corr-003",
        event_time=datetime(2026, 1, 1, 9, 17, tzinfo=UTC),
    )
    events = actor.handle(dup_cancel)
    assert len(events) == 0


def test_trip_kill_switch_cancels_all_open_orders(actor, sample_request):
    # Place two orders
    place_cmd1 = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    actor.handle(place_cmd1)

    place_cmd2 = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-002",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    actor.handle(place_cmd2)

    # Trip kill switch via command
    kill_cmd = TripKillSwitchCommand(
        reason="risk_breach",
        correlation_id="corr-003",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    events = actor.handle(kill_cmd)

    # Should have KillSwitchTripped + 2 OrderCancelled
    kill_events = [e for e in events if e.type == "KillSwitchTripped"]
    assert len(kill_events) == 1
    cancelled = [e for e in events if e.type == "OrderCancelled"]
    assert len(cancelled) == 2


def test_snapshot_returns_current_state(actor, sample_request):
    # Place order
    place_cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    actor.handle(place_cmd)

    snapshot = actor.snapshot()
    assert "orders" in snapshot
    assert "positions" in snapshot
    assert "kill_switch" in snapshot
    assert snapshot["kill_switch"] is False
    assert len(snapshot["orders"]) == 1


def test_idempotency_same_correlation_id_returns_same_events(actor, sample_request):
    cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    events1 = actor.handle(cmd)

    # Same command again — should return same events (idempotent)
    events2 = actor.handle(cmd)
    assert len(events2) == len(events1)
    assert events2[0].event_id == events1[0].event_id


def test_partial_fill_then_full_fill(actor, sample_request):
    # Place order
    place_cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    place_events = actor.handle(place_cmd)
    order_id = place_events[0].payload["order_id"]

    # Partial fill
    fill1 = ApplyFillCommand(
        order_id=order_id,
        cumulative_filled=Decimal("3"),
        fill_price=Decimal("2500"),
        fill_id="trade-001",
        correlation_id="corr-002",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    actor.handle(fill1)
    assert actor._orders[order_id].status == OrderState.PARTIALLY_FILLED

    # Full fill
    fill2 = ApplyFillCommand(
        order_id=order_id,
        cumulative_filled=Decimal("10"),
        fill_price=Decimal("2501"),
        fill_id="trade-002",
        correlation_id="corr-003",
        event_time=datetime(2026, 1, 1, 9, 17, tzinfo=UTC),
    )
    events = actor.handle(fill2)
    assert actor._orders[order_id].status == OrderState.FILLED

    filled = [e for e in events if e.type == "OrderFilled"]
    assert len(filled) == 1
    assert filled[0].payload["fill_quantity"] == "7"  # 10 - 3
    assert filled[0].payload["is_complete"] is True


def test_position_update_accumulates(actor, sample_request):
    # Place order
    place_cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    place_events = actor.handle(place_cmd)
    order_id = place_events[0].payload["order_id"]

    # First fill
    fill1 = ApplyFillCommand(
        order_id=order_id,
        cumulative_filled=Decimal("5"),
        fill_price=Decimal("2500"),
        fill_id="trade-001",
        correlation_id="corr-002",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    actor.handle(fill1)

    # Second fill
    fill2 = ApplyFillCommand(
        order_id=order_id,
        cumulative_filled=Decimal("10"),
        fill_price=Decimal("2502"),
        fill_id="trade-002",
        correlation_id="corr-003",
        event_time=datetime(2026, 1, 1, 9, 17, tzinfo=UTC),
    )
    events = actor.handle(fill2)

    position = [e for e in events if e.type == "PositionUpdated"]
    assert len(position) == 1
    assert position[0].payload["net_quantity"] == "10"
    # avg_price should be weighted average: (2500*5 + 2502*5) / 10 = 2501
    assert position[0].payload["avg_price"] == "2501"


def test_recovery_restores_filled_order_state(actor, sample_request):
    # Place order and fully fill
    place_cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    place_events = actor.handle(place_cmd)
    order_id = place_events[0].payload["order_id"]

    fill_cmd = ApplyFillCommand(
        order_id=order_id,
        cumulative_filled=Decimal("10"),
        fill_price=Decimal("2500"),
        fill_id="trade-001",
        correlation_id="corr-002",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    actor.handle(fill_cmd)

    # Recover
    new_actor = OrderBookActor(session_id="test-sess", event_store=actor._store)
    new_actor.recover()

    assert len(new_actor._orders) == 1
    assert new_actor._orders[order_id].status == OrderState.FILLED
    assert new_actor._orders[order_id].filled_quantity == Decimal("10")
    assert "NSE:RELIANCE" in new_actor._positions


def test_recovery_restores_kill_switch_state(actor, sample_request):
    # Place order, then trip kill switch
    place_cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    actor.handle(place_cmd)

    kill_cmd = TripKillSwitchCommand(
        reason="test",
        correlation_id="corr-002",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    actor.handle(kill_cmd)

    # Recover
    new_actor = OrderBookActor(session_id="test-sess", event_store=actor._store)
    new_actor.recover()

    assert new_actor._kill_switch is True
    assert new_actor.snapshot()["kill_switch"] is True


# =============================================================================
# New Tests — Recovery Flow Fixes
# =============================================================================

def test_recovery_registers_synthetic_orders(actor):
    """SyntheticOrderCreated events must be replayed into state during recovery.

    Before fix: _apply_event had no branch for SyntheticOrderCreated,
    so after recovery synthetic orders were lost and state diverged from event log.
    """
    fill_cmd = ApplyFillCommand(
        order_id="external-ord-1",
        cumulative_filled=Decimal("5"),
        fill_price=Decimal("2500"),
        fill_id="trade-ext-001",
        correlation_id="corr-ext-001",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    actor.handle(fill_cmd)

    # Verify synthetic order exists in runtime state
    assert "external-ord-1" in actor._orders
    assert actor._orders["external-ord-1"].side == "UNKNOWN"

    # Recover into a fresh actor
    new_actor = OrderBookActor(session_id="test-sess", event_store=actor._store)
    new_actor.recover()

    # Synthetic order MUST be present after recovery
    assert "external-ord-1" in new_actor._orders
    assert new_actor._orders["external-ord-1"].side == "UNKNOWN"
    # After recovery, the OrderFilled event is also replayed, so status is FILLED
    assert new_actor._orders["external-ord-1"].status == OrderState.FILLED
    assert new_actor._orders["external-ord-1"].filled_quantity == Decimal("5")


def test_recovery_rebuilds_idempotency_map(actor, sample_request):
    """After recovery, commands with previously-seen correlation_ids must NOT
    create duplicate orders — they should return cached events.

    Before fix: recover() never rebuilt self._idempotency, so a command with
    a correlation_id that was already processed would create a new order.
    """
    # Place order with correlation_id "corr-001"
    place_cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    original_events = actor.handle(place_cmd)
    assert len(original_events) == 1
    assert original_events[0].type == "OrderPlaced"

    # Recover into a fresh actor
    new_actor = OrderBookActor(session_id="test-sess", event_store=actor._store)
    new_actor.recover()

    # Idempotency map must contain the correlation_id
    assert "corr-001" in new_actor._idempotency

    # Re-processing the same command must return cached events, NOT create a new order
    dup_events = new_actor.handle(place_cmd)
    assert len(dup_events) == 1
    assert dup_events[0].event_id == original_events[0].event_id  # Same event returned
    assert len(new_actor._orders) == 1  # Still only 1 order, no duplicate


def test_recovery_idempotency_with_multiple_commands(actor, sample_request):
    """Idempotency map must track ALL correlation_ids from the event log."""
    # Place order
    place_cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-place",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    place_events = actor.handle(place_cmd)
    order_id = place_events[0].payload["order_id"]

    # Apply fill
    fill_cmd = ApplyFillCommand(
        order_id=order_id,
        cumulative_filled=Decimal("5"),
        fill_price=Decimal("2500"),
        fill_id="trade-001",
        correlation_id="corr-fill",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    actor.handle(fill_cmd)

    # Recover
    new_actor = OrderBookActor(session_id="test-sess", event_store=actor._store)
    new_actor.recover()

    # Both correlation_ids must be in idempotency map
    assert "corr-place" in new_actor._idempotency
    assert "corr-fill" in new_actor._idempotency

    # Re-processing either command returns cached events
    dup_place = new_actor.handle(place_cmd)
    assert dup_place[0].event_id == place_events[0].event_id


def test_recovery_idempotency_excludes_rejections(actor, sample_request):
    """OrderRejected events must NOT be registered in the idempotency map,
    so that rejected commands can be retried."""
    # Trip kill switch
    actor.trip_kill_switch("test")

    # Place order — will be rejected
    place_cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-rejected",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    events = actor.handle(place_cmd)
    assert events[0].type == "OrderRejected"

    # Recover
    new_actor = OrderBookActor(session_id="test-sess", event_store=actor._store)
    new_actor.recover()

    # Rejected correlation_id must NOT be in idempotency map
    assert "corr-rejected" not in new_actor._idempotency


def test_handle_apply_fill_catches_invalid_state_transition_only(actor):
    """_handle_apply_fill must catch InvalidStateTransition, not bare Exception.

    This ensures bugs (e.g., KeyError, TypeError) are not silently swallowed.
    """
    from tradex_trading.events.order_fsm import InvalidStateTransition

    # Create a scenario where transition raises InvalidStateTransition:
    # An order in FILLED state receiving another fill
    fill_cmd = ApplyFillCommand(
        order_id="unknown-ord",
        cumulative_filled=Decimal("5"),
        fill_price=Decimal("2500"),
        fill_id="trade-001",
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    # First fill creates synthetic order and fills it
    events1 = actor.handle(fill_cmd)
    assert len(events1) == 2  # SyntheticOrderCreated + OrderFilled

    # Second fill with higher cumulative — order is already FILLED (qty=5, cum=5)
    # This triggers the InvalidStateTransition fallback
    fill_cmd2 = ApplyFillCommand(
        order_id="unknown-ord",
        cumulative_filled=Decimal("10"),
        fill_price=Decimal("2501"),
        fill_id="trade-002",
        correlation_id="corr-002",
        event_time=datetime(2026, 1, 1, 9, 17, tzinfo=UTC),
    )
    # This should NOT raise — it should use the fallback
    events2 = actor.handle(fill_cmd2)
    assert len(events2) == 1  # OrderFilled only (no synthetic)
    assert events2[0].type == "OrderFilled"


def test_state_mutation_after_persist_in_place_order(actor, sample_request, monkeypatch):
    """If store.append() fails, state must NOT be mutated.

    This test verifies the ordering: compute event, persist, then mutate state.
    """
    from tradex_trading.events.store import Event

    place_cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-fail",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )

    # First call succeeds, second (for idempotency) would return cached
    # We need to test the failure case — patch append to raise on first call
    original_append = actor._store.append
    call_count = 0

    def failing_append(event):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("Disk full")
        return original_append(event)

    actor._store.append = failing_append

    # When append fails, the exception should propagate and state should NOT be mutated
    with pytest.raises(RuntimeError, match="Disk full"):
        actor.handle(place_cmd)

    # State must NOT be mutated since persist failed
    assert len(actor._orders) == 0
    assert "corr-fail" not in actor._idempotency


def test_state_mutation_after_persist_in_apply_fill(actor, sample_request, monkeypatch):
    """If store.append() fails during fill handling, state must NOT be mutated."""
    # Place a real order first
    place_cmd = PlaceOrderCommand(
        request=sample_request,
        correlation_id="corr-001",
        event_time=datetime(2026, 1, 1, 9, 15, tzinfo=UTC),
    )
    place_events = actor.handle(place_cmd)
    order_id = place_events[0].payload["order_id"]

    # Now attempt a fill, but make the OrderFilled event append fail
    original_append = actor._store.append
    call_count = 0

    def failing_append(event):
        nonlocal call_count
        call_count += 1
        # 1st call from fill handler = OrderFilled event (FAILS)
        if call_count == 1:
            raise RuntimeError("Disk full")
        return original_append(event)

    actor._store.append = failing_append

    fill_cmd = ApplyFillCommand(
        order_id=order_id,
        cumulative_filled=Decimal("5"),
        fill_price=Decimal("2500"),
        fill_id="trade-001",
        correlation_id="corr-002",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )

    with pytest.raises(RuntimeError, match="Disk full"):
        actor.handle(fill_cmd)

    # Order state must NOT be updated since persist failed
    assert actor._orders[order_id].filled_quantity == Decimal("0")
    assert actor._orders[order_id].status == OrderState.ACK


def test_recovery_preserves_synthetic_order_after_subsequent_fill(actor):
    """After recovery, a synthetic order that received a fill must have correct state."""
    # Create synthetic order via unknown order fill
    fill_cmd = ApplyFillCommand(
        order_id="ext-ord",
        cumulative_filled=Decimal("10"),
        fill_price=Decimal("2500"),
        fill_id="trade-ext",
        correlation_id="corr-ext",
        event_time=datetime(2026, 1, 1, 9, 16, tzinfo=UTC),
    )
    events = actor.handle(fill_cmd)

    # Should have SyntheticOrderCreated + OrderFilled
    assert any(e.type == "SyntheticOrderCreated" for e in events)
    assert any(e.type == "OrderFilled" for e in events)

    # Recover
    new_actor = OrderBookActor(session_id="test-sess", event_store=actor._store)
    new_actor.recover()

    # Synthetic order must exist with correct filled state
    assert "ext-ord" in new_actor._orders
    assert new_actor._orders["ext-ord"].filled_quantity == Decimal("10")
    assert new_actor._orders["ext-ord"].status == OrderState.FILLED
