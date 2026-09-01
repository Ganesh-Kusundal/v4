"""Tests for Unified Trading Session — single entry point for all modes.

All tests use real EventStore, real OrderBookActor, real CommandProcessor.
No mocking — these are integration tests verifying the unified pipeline.
"""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradex_domain import Equity, OrderRequest, OrderSide, OrderType, Price, Quantity
from tradex_trading.events.fill_matcher import BrokerOrderUpdate
from tradex_trading.events.processor import CommandResult
from tradex_trading.events.projectors import OrderView, PositionView
from tradex_trading.events.risk_engine import RiskConfig
from tradex_trading.events.session import (
    BrokerConfig,
    DataSourceConfig,
    SessionConfig,
    TradingSession,
)


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def tmp_db():
    """Create a temporary SQLite database for the event store."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    yield path
    os.unlink(path)


@pytest.fixture
def paper_config(tmp_db):
    """SessionConfig for paper mode."""
    return SessionConfig(
        session_id="paper-test-001",
        mode="paper",
        event_store_path=tmp_db,
        risk_config=RiskConfig(
            max_order_value=1_000_000.0,
            max_position_value=5_000_000.0,
            max_orders_per_minute=100,
            max_daily_loss=50_000.0,
        ),
        data_source=DataSourceConfig(
            type="simulated",
            broker=BrokerConfig(
                broker_id="paper",
                client_id="paper-client",
                access_token="paper-token",
            ),
        ),
    )


@pytest.fixture
def backtest_config(tmp_db):
    """SessionConfig for backtest mode."""
    return SessionConfig(
        session_id="backtest-001",
        mode="backtest",
        event_store_path=tmp_db,
        risk_config=RiskConfig(
            max_order_value=1_000_000.0,
            max_position_value=5_000_000.0,
            max_orders_per_minute=1000,
            max_daily_loss=500_000.0,
        ),
        data_source=DataSourceConfig(
            type="historical",
            historical_path="data/ohlcv/",
        ),
    )


@pytest.fixture
def replay_config(tmp_db):
    """SessionConfig for replay mode."""
    return SessionConfig(
        session_id="replay-001",
        mode="replay",
        event_store_path=tmp_db,
        risk_config=RiskConfig(
            max_order_value=1_000_000.0,
            max_position_value=5_000_000.0,
            max_orders_per_minute=100,
            max_daily_loss=50_000.0,
        ),
        data_source=DataSourceConfig(
            type="replay",
            historical_path="data/ohlcv/",
            replay_speed=2.0,
        ),
    )


@pytest.fixture
def live_config(tmp_db):
    """SessionConfig for live mode."""
    return SessionConfig(
        session_id="live-001",
        mode="live",
        event_store_path=tmp_db,
        risk_config=RiskConfig(
            max_order_value=500_000.0,
            max_position_value=2_000_000.0,
            max_orders_per_minute=20,
            max_daily_loss=25_000.0,
        ),
        data_source=DataSourceConfig(
            type="broker",
            broker=BrokerConfig(
                broker_id="dhan",
                client_id="test-client-id",
                access_token="test-access-token",
            ),
        ),
    )


def make_request(
    instrument: str = "NSE:RELIANCE",
    side: OrderSide = OrderSide.BUY,
    quantity: str = "10",
    price: str = "2500",
    correlation_id: str = "corr-001",
) -> OrderRequest:
    """Helper to create an OrderRequest."""
    parts = instrument.split(":")
    return OrderRequest(
        instrument=Equity.of(parts[0], parts[1]),
        side=side,
        order_type=OrderType.LIMIT,
        quantity=Quantity(Decimal(quantity)),
        price=Price(Decimal(price)),
        correlation_id=correlation_id,
    )


# =============================================================================
# Tests
# =============================================================================


def test_session_start_and_stop(paper_config):
    """Create session, start, stop, verify clean state."""
    session = TradingSession(paper_config)

    # Before start
    assert session.state == "NEW"

    # Start
    session.start()
    assert session.state == "READY"

    # Stop
    session.stop()
    assert session.state == "STOPPED"


def test_session_start_is_idempotent(paper_config):
    """Calling start() twice should not raise."""
    session = TradingSession(paper_config)
    session.start()
    session.start()  # Should be a no-op
    assert session.state == "READY"
    session.stop()


def test_session_stop_is_idempotent(paper_config):
    """Calling stop() twice should not raise."""
    session = TradingSession(paper_config)
    session.start()
    session.stop()
    session.stop()  # Should be a no-op
    assert session.state == "STOPPED"


def test_place_order_in_paper_mode(paper_config):
    """Paper mode, place order, verify CommandResult.

    Paper mode simulates an instant fill at the order's limit price, so a
    successful placement emits OrderPlaced followed by OrderFilled +
    PositionUpdated via the synchronous paper fill callback.
    """
    session = TradingSession(paper_config)
    session.start()

    request = make_request(correlation_id="corr-place-001")
    result = session.place_order(request)

    assert result.success is True
    assert result.is_duplicate is False
    assert result.error is None
    assert len(result.events) >= 1
    assert result.events[0].type == "OrderPlaced"
    assert result.correlation_id == "corr-place-001"

    # Paper mode fills instantly: the order read model reflects the fill
    # (OrderFilled/PositionUpdated were emitted via the paper callback and
    # applied to projectors before this method returned).
    order_id = result.events[0].payload["order_id"]
    order = session.get_orders()[0]
    assert order.order_id == order_id
    assert order.status == "FILLED"
    assert order.filled_quantity == Decimal("10")

    session.stop()


def test_place_order_before_start_raises(paper_config):
    """Placing an order before start() should raise RuntimeError."""
    session = TradingSession(paper_config)

    request = make_request(correlation_id="corr-early")
    with pytest.raises(RuntimeError, match="not running"):
        session.place_order(request)


def test_cancel_order_in_paper_mode(paper_config):
    """Paper mode: cancel after instant fill is a no-op.

    Paper mode fills orders instantly at placement, so the order is terminal
    (FILLED) by the time cancel_order is called. Cancelling a terminal order
    emits no events per the order FSM. The cancel still succeeds (the command
    was processed) but the order remains FILLED.
    """
    session = TradingSession(paper_config)
    session.start()

    # Place order (paper mode fills instantly)
    request = make_request(correlation_id="corr-cancel-001")
    place_result = session.place_order(request)
    assert place_result.success is True

    # Get the order_id from the event
    order_id = place_result.events[0].payload["order_id"]

    # Cancel order — no-op because the order is already FILLED
    cancel_result = session.cancel_order(order_id)
    assert cancel_result.success is True
    assert len(cancel_result.events) == 0

    # The order remains FILLED, not cancelled
    order = session.get_orders()[0]
    assert order.status == "FILLED"

    session.stop()


def test_cancel_unknown_order_returns_no_events(paper_config):
    """Cancelling an unknown order should return success with no events."""
    session = TradingSession(paper_config)
    session.start()

    result = session.cancel_order("nonexistent-order-id")
    assert result.success is True
    assert len(result.events) == 0

    session.stop()


def test_get_orders_returns_placed_orders(paper_config):
    """Place orders, verify get_orders returns them."""
    session = TradingSession(paper_config)
    session.start()

    # Place two orders
    req1 = make_request(correlation_id="corr-get-001")
    req2 = make_request(correlation_id="corr-get-002", quantity="20")
    session.place_order(req1)
    session.place_order(req2)

    orders = session.get_orders()
    assert len(orders) == 2

    # Verify they are OrderView instances
    assert all(isinstance(o, OrderView) for o in orders)

    # Paper mode fills instantly: quantities reflect the placed amounts
    quantities = {o.filled_quantity for o in orders}
    assert Decimal("10") in quantities
    assert Decimal("20") in quantities

    session.stop()


def test_get_positions_after_fill(paper_config):
    """Place order, apply fill, verify position."""
    session = TradingSession(paper_config)
    session.start()

    # Place order
    request = make_request(correlation_id="corr-fill-001", quantity="10", price="2500")
    place_result = session.place_order(request)
    order_id = place_result.events[0].payload["order_id"]

    # Apply fill through the session's unified fill entry point
    fill_result = session.apply_fill(
        order_id=order_id,
        cumulative_filled=Decimal("10"),
        fill_price=Decimal("2500"),
        fill_id="fill-001",
    )
    assert fill_result.success is True

    # Verify position
    positions = session.get_positions()
    assert len(positions) == 1
    pos = positions[0]
    assert isinstance(pos, PositionView)
    assert pos.instrument == "NSE:RELIANCE"
    assert pos.quantity == Decimal("10")
    assert pos.avg_price == Decimal("2500")

    session.stop()


def test_backtest_mode_uses_historical_data(backtest_config):
    """Backtest mode, verify data source configured correctly."""
    session = TradingSession(backtest_config)
    session.start()

    # Verify mode and data source
    assert session.mode == "backtest"
    assert session.data_source.type == "historical"
    assert session.data_source.historical_path == "data/ohlcv/"

    # Session should be functional — place an order
    request = make_request(correlation_id="corr-bt-001")
    result = session.place_order(request)
    assert result.success is True

    session.stop()


def test_replay_mode_uses_event_log(replay_config):
    """Replay mode, verify events are replayed."""
    session = TradingSession(replay_config)
    session.start()

    # Verify mode and data source
    assert session.mode == "replay"
    assert session.data_source.type == "replay"
    assert session.data_source.replay_speed == 2.0

    # Place an order to generate events
    request = make_request(correlation_id="corr-replay-001")
    result = session.place_order(request)
    assert result.success is True

    # Verify events are in the store
    orders = session.get_orders()
    assert len(orders) == 1

    session.stop()


def test_live_mode_configures_broker_data_source(live_config):
    """Live mode, verify broker data source is configured."""
    session = TradingSession(live_config)
    session.start()

    assert session.mode == "live"
    assert session.data_source.type == "broker"
    assert session.data_source.broker is not None
    assert session.data_source.broker.broker_id == "dhan"

    session.stop()


def test_live_fill_updates_read_models_without_restart(live_config):
    """Live mode: broker fills via FillMatcher must update the session's read
    models (orders + positions) immediately — not only after a restart/rebuild.

    Regression test for the live projector-bypass bug: FillMatcher previously
    sent ApplyFillCommands straight to the CommandProcessor, persisting
    OrderFilled/PositionUpdated to the event store while leaving the session's
    projectors untouched.
    """
    session = TradingSession(live_config)
    session.start()

    # Place an order (registered with the FillMatcher by on_order_placed)
    request = make_request(correlation_id="corr-live-fill-001")
    place_result = session.place_order(request)
    assert place_result.success is True
    order_id = place_result.events[0].payload["order_id"]

    # Sanity: read models show the placed (unfilled) order
    order = session.get_orders()[0]
    assert order.status == "ACK"
    assert order.filled_quantity == Decimal("0")
    assert session.get_positions() == []

    # Feed a broker fill through the live data source path
    broker_order_id = f"broker-{order_id[:8]}"  # deterministic mapping from on_order_placed
    session.fill_matcher.process_update(
        BrokerOrderUpdate(
            broker_order_id=broker_order_id,
            instrument="NSE:RELIANCE",
            side="BUY",
            quantity=10,
            filled_quantity=10,
            fill_price=Decimal("2500"),
            status="FILLED",
            timestamp=datetime.now(UTC),
        )
    )

    # Read models must reflect the fill WITHOUT a session restart
    order = session.get_orders()[0]
    assert order.status == "FILLED"
    assert order.filled_quantity == Decimal("10")

    positions = session.get_positions()
    assert len(positions) == 1
    assert positions[0].instrument == "NSE:RELIANCE"
    assert positions[0].quantity == Decimal("10")
    assert positions[0].avg_price == Decimal("2500")

    session.stop()


def test_session_recovers_from_event_log(paper_config):
    """After stop and restart, session should recover state from event log."""
    # First session — place orders
    session1 = TradingSession(paper_config)
    session1.start()

    req = make_request(correlation_id="corr-recover-001")
    session1.place_order(req)
    assert len(session1.get_orders()) == 1

    session1.stop()

    # Second session — same session_id, same DB
    session2 = TradingSession(paper_config)
    session2.start()

    # Should recover the order from event log
    orders = session2.get_orders()
    assert len(orders) == 1
    assert orders[0].instrument == "NSE:RELIANCE"

    session2.stop()


def test_duplicate_order_idempotency(paper_config):
    """Same correlation_id should return duplicate result."""
    session = TradingSession(paper_config)
    session.start()

    request = make_request(correlation_id="corr-dup-001")
    result1 = session.place_order(request)
    result2 = session.place_order(request)

    assert result1.success is True
    assert result1.is_duplicate is False
    assert result2.is_duplicate is True
    assert result2.success is True

    # Only one order should exist
    orders = session.get_orders()
    assert len(orders) == 1

    session.stop()


def test_get_orders_empty_when_no_orders(paper_config):
    """get_orders should return empty list when no orders placed."""
    session = TradingSession(paper_config)
    session.start()

    orders = session.get_orders()
    assert orders == []

    session.stop()


def test_get_positions_empty_when_no_fills(paper_config):
    """get_positions should return empty list when no fills applied."""
    session = TradingSession(paper_config)
    session.start()

    positions = session.get_positions()
    assert positions == []

    session.stop()


def test_session_config_validation():
    """SessionConfig should validate mode values."""
    with pytest.raises(ValueError, match="Invalid mode"):
        SessionConfig(
            session_id="test",
            mode="invalid_mode",
            event_store_path=":memory:",
            risk_config=RiskConfig(
                max_order_value=1000.0,
                max_position_value=5000.0,
                max_orders_per_minute=10,
                max_daily_loss=1000.0,
            ),
            data_source=DataSourceConfig(type="simulated"),
        )


def test_data_source_config_validation():
    """DataSourceConfig should validate type values."""
    with pytest.raises(ValueError, match="Invalid data source type"):
        DataSourceConfig(type="invalid_type")


def test_trip_kill_switch_rejects_new_orders(paper_config):
    """Tripping the kill switch through the session rejects all new orders."""
    session = TradingSession(paper_config)
    session.start()

    result = session.trip_kill_switch("manual test halt")
    assert result.success is True
    assert any(e.type == "KillSwitchTripped" for e in result.events)

    # New orders must be rejected while the kill switch is active
    request = make_request(correlation_id="corr-kill-001")
    place_result = session.place_order(request)
    assert place_result.success is False
    assert place_result.error is not None
    assert "kill_switch" in place_result.error

    session.stop()


def test_kill_switch_survives_recovery(paper_config):
    """Kill switch state must persist across session restart (event log replay)."""
    session1 = TradingSession(paper_config)
    session1.start()
    session1.trip_kill_switch("halt before restart")

    session1.stop()

    # Restart with same session_id and DB — the kill switch must still be tripped
    session2 = TradingSession(paper_config)
    session2.start()

    request = make_request(correlation_id="corr-kill-recover-001")
    place_result = session2.place_order(request)
    assert place_result.success is False
    assert "kill_switch" in place_result.error

    session2.stop()
