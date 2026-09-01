"""Tests for Unified Trading Session — single entry point for all modes.

All tests use real EventStore, real OrderBookActor, real CommandProcessor.
No mocking — these are integration tests verifying the unified pipeline.
"""

from __future__ import annotations

import os
import tempfile
import threading
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


def make_rate_config(tmp_db, max_orders_per_minute: int) -> SessionConfig:
    """SessionConfig (paper mode) with a small per-minute rate limit."""
    return SessionConfig(
        session_id=f"rate-{max_orders_per_minute}",
        mode="paper",
        event_store_path=tmp_db,
        risk_config=RiskConfig(
            max_order_value=1_000_000.0,
            max_position_value=5_000_000.0,
            max_orders_per_minute=max_orders_per_minute,
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


def test_duplicate_retry_bypasses_risk_gate(tmp_db):
    """A duplicate retry returns the cached result, not a fresh risk verdict.

    Scenario: rate limit max=2. Orders A and B are placed (both approved).
    Retrying A with the same correlation_id must return A's cached success
    (is_duplicate=True) — NOT a rate-limit rejection, because A already
    processed. Risk checks run only for genuinely new commands.
    """
    config = make_rate_config(tmp_db, max_orders_per_minute=2)
    session = TradingSession(config)
    session.start()

    req_a = make_request(correlation_id="corr-dup-risk-001")
    req_b = make_request(correlation_id="corr-dup-risk-002")
    result_a1 = session.place_order(req_a)
    result_b = session.place_order(req_b)
    assert result_a1.success is True
    assert result_b.success is True

    # Retry A — the rate window is now full (2 orders), but A already
    # processed, so the retry must return the cached result.
    result_a2 = session.place_order(req_a)

    assert result_a2.success is True
    assert result_a2.is_duplicate is True
    assert result_a2.error is None
    # Same events as the original placement
    assert result_a2.events[0].event_id == result_a1.events[0].event_id

    # Still only 2 orders — no duplicate was created
    assert len(session.get_orders()) == 2

    session.stop()


def test_rate_limit_rejects_order_beyond_max(tmp_db):
    """Session-level rate limit counts the candidate order.

    With max_orders_per_minute=2, placing a third distinct order within the
    same minute must be rejected — the limit bounds orders *including* the
    one being placed, not orders already placed.
    """
    config = make_rate_config(tmp_db, max_orders_per_minute=2)
    session = TradingSession(config)
    session.start()

    r1 = session.place_order(make_request(correlation_id="corr-rate-001"))
    r2 = session.place_order(make_request(correlation_id="corr-rate-002"))
    r3 = session.place_order(make_request(correlation_id="corr-rate-003"))

    assert r1.success is True
    assert r2.success is True
    # The third order is the 3rd within the minute: 2 in window + candidate
    assert r3.success is False
    assert r3.error is not None
    assert "rate" in r3.error.lower()

    # Only the two approved orders exist
    assert len(session.get_orders()) == 2

    session.stop()


def test_rate_limit_boundary_allows_max_orders(tmp_db):
    """With max_orders_per_minute=2, the second order is still approved.

    The candidate is counted: 1 recent + candidate == 2 <= max → approved.
    """
    config = make_rate_config(tmp_db, max_orders_per_minute=2)
    session = TradingSession(config)
    session.start()

    r1 = session.place_order(make_request(correlation_id="corr-rate-bound-001"))
    r2 = session.place_order(make_request(correlation_id="corr-rate-bound-002"))

    assert r1.success is True
    assert r2.success is True
    assert len(session.get_orders()) == 2

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


# =============================================================================
# Thread safety (review fix 13)
# =============================================================================


def test_concurrent_same_correlation_places_exactly_one_order(tmp_db):
    """8 threads place the SAME order (same correlation_id) concurrently.

    The idempotency cache is check-then-cache: without a lock, two or more
    threads can both miss the cache, both pass the risk gate, and both run
    the processor before either caches the result — producing duplicate
    OrderPlaced events and double position state in the read model.
    """
    config = make_rate_config(tmp_db, max_orders_per_minute=100)
    session = TradingSession(config)
    session.start()

    request = make_request(correlation_id="corr-race-001")
    results: list = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        result = session.place_order(request)
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 8
    # Exactly one worker executed the command; the rest got cached results.
    assert sum(1 for r in results if r.success and not r.is_duplicate) == 1
    assert sum(1 for r in results if r.is_duplicate) == 7

    # Read model: exactly one order, one open position of 10.
    orders = session.get_orders()
    assert len(orders) == 1
    assert orders[0].status == "FILLED"  # paper mode auto-fills

    positions = session.get_positions()
    assert len(positions) == 1
    assert positions[0].quantity == Decimal("10")

    session.stop()


def test_concurrent_same_cumulative_fill_applies_once(tmp_db):
    """2 threads apply the SAME cumulative fill to one order concurrently.

    The actor computes delta = cumulative_filled - order.filled_quantity
    then mutates: two threads can both read filled_quantity=0, both compute
    delta=10, and both emit OrderFilled(fill_quantity=10) — double-crediting
    the position and double-persisting the fill.
    """
    config = SessionConfig(
        session_id="fill-race-001",
        mode="backtest",  # no auto-fill; fills come from apply_fill
        event_store_path=tmp_db,
        risk_config=RiskConfig(
            max_order_value=1_000_000.0,
            max_position_value=5_000_000.0,
            max_orders_per_minute=100,
            max_daily_loss=50_000.0,
        ),
        data_source=DataSourceConfig(
            type="historical",
            historical_path="data/ohlcv/",
        ),
    )
    session = TradingSession(config)
    session.start()

    place = session.place_order(make_request(correlation_id="corr-fill-race"))
    assert place.success

    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        session.apply_fill(
            order_id=place.events[0].payload["order_id"],
            cumulative_filled=Decimal("10"),
            fill_price=Decimal("2500"),
            fill_id="fill-race-001",
        )

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Order read model: single fill, cumulative 10, not 20.
    orders = session.get_orders()
    assert len(orders) == 1
    assert orders[0].filled_quantity == Decimal("10")

    # Position: 10 shares, not 20.
    positions = session.get_positions()
    assert len(positions) == 1
    assert positions[0].quantity == Decimal("10")

    # Event log: exactly one OrderFilled event for this order.
    events = session._store.read_all(config.session_id)
    order_id = place.events[0].payload["order_id"]
    filled_events = [
        e
        for e in events
        if e.type == "OrderFilled" and e.payload["order_id"] == order_id
    ]
    assert len(filled_events) == 1

    session.stop()


def test_concurrent_place_respects_rate_limit_under_threads(tmp_db):
    """4 threads place distinct orders concurrently with max_orders_per_minute=3.

    Rate limiting reads then appends to _order_timestamps: without a lock,
    concurrent placements can all observe an empty window and all pass,
    allowing more orders than the configured maximum.
    """
    config = make_rate_config(tmp_db, max_orders_per_minute=3)
    session = TradingSession(config)
    session.start()

    results: list = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(4)

    def worker(i: int):
        barrier.wait()
        result = session.place_order(make_request(correlation_id=f"corr-rl-{i}"))
        with results_lock:
            results.append(result)

    threads = [
        threading.Thread(target=worker, args=(i,)) for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 4
    approved = sum(1 for r in results if r.success)
    rejected = sum(1 for r in results if not r.success)
    assert approved == 3
    assert rejected == 1
    assert "Risk check failed" in results[[r.success for r in results].index(False)].error

    # Read model: exactly 3 orders.
    assert len(session.get_orders()) == 3

    session.stop()
