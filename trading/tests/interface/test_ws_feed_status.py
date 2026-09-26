"""``feed_status`` WebSocket message — the live-order gate for the UI.

The UI must never enable a live order control while the feed is not READY.
The backend single source of truth is ``_helpers.feed_status_payload``; these
tests pin both the pull path (explicit request, subscribe ack) and the shape.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from tradex_trading.interface._helpers import feed_status_payload
from tradex_trading.interface.fastapi_app import create_app
from tradex_trading.runtime.feed_supervisor import FeedSupervisor
from tradex_trading.runtime.metrics import MetricsRegistry
from tradex_trading.sdk.session import TradingSession


def _session_with_feed(state: str) -> TradingSession:
    session = TradingSession.paper()
    supervisor = FeedSupervisor()
    supervisor.connected()
    supervisor.recovery_started()
    if state == "ready":
        now = datetime.now(UTC)
        supervisor.recovery_succeeded(last_event_at=now, recovered_through=now)
    session._feed_supervisor = supervisor  # wired by boot() in live mode
    return session


def test_feed_status_payload_without_supervisor_keeps_controls_enabled() -> None:
    payload = feed_status_payload(None)

    assert payload["type"] == "feed_status"
    assert payload["state"] is None
    assert payload["ready"] is None
    assert payload["live_orders_enabled"] is True


def test_feed_status_payload_reports_gate() -> None:
    session = _session_with_feed("resynchronizing")
    try:
        payload = feed_status_payload(session)
    finally:
        session.stop()

    assert payload["state"] == "resynchronizing"
    assert payload["ready"] is False
    assert payload["generation"] == 1
    assert payload["live_orders_enabled"] is False


def test_feed_status_request_returns_the_gate() -> None:
    session = _session_with_feed("resynchronizing")
    app = create_app(session=session)
    try:
        with TestClient(app).websocket_connect("/ws/stream") as ws:
            ws.send_json({"type": "feed_status"})
            frame = ws.receive_json()
    finally:
        session.stop()

    assert frame["type"] == "feed_status"
    assert frame["state"] == "resynchronizing"
    assert frame["ready"] is False
    assert frame["live_orders_enabled"] is False
    assert frame["generation"] == 1


def test_feed_status_request_ready_feed_enables_controls() -> None:
    session = _session_with_feed("ready")
    app = create_app(session=session)
    try:
        with TestClient(app).websocket_connect("/ws/stream") as ws:
            ws.send_json({"type": "feed_status"})
            frame = ws.receive_json()
    finally:
        session.stop()

    assert frame["ready"] is True
    assert frame["live_orders_enabled"] is True
    assert frame["age_seconds"] is not None


def test_subscribed_ack_carries_feed_gate() -> None:
    session = _session_with_feed("resynchronizing")
    app = create_app(session=session)
    try:
        with TestClient(app).websocket_connect("/ws/stream") as ws:
            ws.send_json(
                {"type": "subscribe", "instruments": ["NSE:RELIANCE"], "depth": "off"}
            )
            ack = ws.receive_json()
    finally:
        session.stop()

    assert ack["type"] == "subscribed"
    assert ack["feed"]["feed_state"] == "resynchronizing"
    assert ack["feed"]["feed_ready"] is False
    assert ack["feed"]["live_orders_enabled"] is False


def test_queue_drops_increment_feed_queue_drops_total() -> None:
    """D: feed_queue_drops_total increments when outbound queue overflows.

    Uses outbound_max=1 to force every second message to be dropped.
    The session carries a MetricsRegistry so the counter is reachable.
    """
    from unittest.mock import MagicMock

    from tradex_trading.execution.engine import ExecutionEngine, MemoryIdempotencyGuard, RiskManager
    from tradex_trading.reactive.bus import ReactiveBus
    from tradex_brokers.paper.adapter import PaperBroker
    from tradex_domain import BrokerId

    metrics = MetricsRegistry()
    bus = ReactiveBus(metrics=metrics)
    broker = PaperBroker()
    broker.connect()
    engine = ExecutionEngine(bus=bus, fill_source=MagicMock(), risk_manager=RiskManager(), metrics=metrics)
    from tradex_trading.execution.trading_cache import TradingCache
    session = TradingSession(
        broker=broker,
        bus=bus,
        engine=engine,
        cache=engine.cache,
        broker_id=BrokerId.PAPER,
        metrics=metrics,
    )
    session.start()

    app = create_app(session=session, outbound_max=1)
    try:
        from tradex_domain.instruments import Equity
        from tradex_domain.market import Quote
        from tradex_domain.value_objects import Price
        from decimal import Decimal

        # Subscribe and flood the socket with many quotes via bus publishes.
        # With outbound_max=1, the second publish into the full queue causes a drop.
        with TestClient(app).websocket_connect("/ws/stream") as ws:
            ws.send_json({"type": "subscribe", "instruments": ["NSE:RELIANCE"], "depth": "off"})
            _ack = ws.receive_json()  # subscribed ack
            inst = Equity.of("NSE", "RELIANCE")
            for _ in range(20):
                bus.publish(Quote(instrument=inst, ltp=Price(value=Decimal("100"))))
            # Drain available frames
            import time
            time.sleep(0.05)
    finally:
        session.stop()

    # At least one drop must have been counted.
    drops = metrics.get("feed_queue_drops_total")
    assert drops is not None and drops >= 1, (
        f"Expected feed_queue_drops_total >= 1, got {drops!r}"
    )
