"""SDK session edge case tests.

Ported from v3 ``test_sdk_session_edges.py``.

v4 API differences:
- ``TradingSession`` requires ``broker, bus, engine, cache, broker_id``
- Services are properties (not methods) that check READY state
- ``StreamService`` has ``subscribe_quotes`` / ``subscribe_fills`` (not orders/positions)
- ``AnalyticsService.indicators()`` is stub
- ``ExtensionService.is_extension_adapter()`` checks protocol conformance
"""

from __future__ import annotations

import pytest
from tradex_domain.errors import CapabilityNotSupportedError, SessionStateError
from tradex_domain.strategy import ScannerDefinition

from tradex_trading.runtime.startup import boot
from tradex_trading.sdk.session import SessionState
from tradex_trading.sdk.streaming import StreamSubscription

# ---------------------------------------------------------------------------
# Session state edges
# ---------------------------------------------------------------------------


class TestSessionStateEdges:
    """Session lifecycle edge cases."""

    def test_services_raise_after_stop(self) -> None:
        session = boot()
        session.stop()
        with pytest.raises(SessionStateError):
            _ = session.market
        with pytest.raises(SessionStateError):
            _ = session.trade
        with pytest.raises(SessionStateError):
            _ = session.portfolio
        with pytest.raises(SessionStateError):
            _ = session.stream
        with pytest.raises(SessionStateError):
            _ = session.scanner
        with pytest.raises(SessionStateError):
            _ = session.analytics
        with pytest.raises(SessionStateError):
            _ = session.extension

    def test_state_property_reflects_lifecycle(self) -> None:
        session = boot()
        assert session.state == SessionState.READY
        session.stop()
        assert session.state == SessionState.STOPPED


# ---------------------------------------------------------------------------
# StreamService — subscription lifecycle
# ---------------------------------------------------------------------------


class TestStreamServiceEdges:
    """StreamService subscription management."""

    def test_subscribe_quotes_returns_subscription(self) -> None:
        session = boot()
        sub = session.stream.subscribe_quotes(lambda q: None)
        assert isinstance(sub, StreamSubscription)
        assert sub.is_active
        session.stop()

    def test_subscribe_fills_returns_subscription(self) -> None:
        session = boot()
        sub = session.stream.subscribe_fills(lambda f: None)
        assert isinstance(sub, StreamSubscription)
        assert sub.is_active
        session.stop()

    def test_subscribe_depth_returns_subscription(self) -> None:
        session = boot()
        sub = session.stream.subscribe_depth(lambda d: None)
        assert isinstance(sub, StreamSubscription)
        assert sub.is_active
        session.stop()

    def test_start_market_feed_raises_in_paper_mode(self) -> None:
        from tradex_domain.instruments import Equity

        session = boot()
        assert session.market_feed is None
        with pytest.raises(CapabilityNotSupportedError, match="paper"):
            session.start_market_feed([Equity.of("NSE", "RELIANCE")])
        session.stop()

    def test_subscribe_depth_receives_published_depth(self) -> None:
        from decimal import Decimal

        from tradex_domain.instruments import Equity
        from tradex_domain.market import Depth
        from tradex_domain.value_objects import Price, Quantity

        session = boot()
        received: list[Depth] = []
        sub = session.stream.subscribe_depth(received.append)
        assert sub.is_active
        depth = Depth(
            instrument=Equity.of("NSE", "RELIANCE"),
            bids=((Price(value=Decimal("1319.0")), Quantity(value=Decimal("10"))),),
            asks=((Price(value=Decimal("1319.5")), Quantity(value=Decimal("5"))),),
        )
        session.bus.publish(depth)
        assert len(received) == 1
        assert received[0].instrument.symbol == "RELIANCE"
        session.stop()

    def test_stop_cancels_subscriptions(self) -> None:
        session = boot()
        sub = session.stream.subscribe_quotes(lambda q: None)
        assert sub.is_active
        session.stop()
        # After stop, subscription should be cancelled
        assert not sub.is_active

    def test_multiple_subscriptions(self) -> None:
        session = boot()
        sub1 = session.stream.subscribe_quotes(lambda q: None)
        sub2 = session.stream.subscribe_fills(lambda f: None)
        assert sub1.is_active
        assert sub2.is_active
        session.stop()
        assert not sub1.is_active
        assert not sub2.is_active


# ---------------------------------------------------------------------------
# ScannerService — stub
# ---------------------------------------------------------------------------


class TestScannerServiceEdges:
    """ScannerService raises loudly without a bound engine."""

    def test_run_without_engine_raises(self) -> None:
        session = boot()
        with pytest.raises(CapabilityNotSupportedError):
            session.scanner.run(ScannerDefinition())
        with pytest.raises(CapabilityNotSupportedError):
            session.scanner.top(ScannerDefinition())
        session.stop()


# ---------------------------------------------------------------------------
# AnalyticsService — stub
# ---------------------------------------------------------------------------


class TestAnalyticsServiceEdges:
    """AnalyticsService is a stub."""

    def test_indicators_returns_series(self) -> None:
        session = boot()
        data = [1.0, 2.0, 3.0]
        with pytest.raises(CapabilityNotSupportedError, match="analytics engine"):
            session.analytics.indicators(data, ["sma"])
        session.stop()


# ---------------------------------------------------------------------------
# ExtensionService
# ---------------------------------------------------------------------------


class TestExtensionServiceEdges:
    """ExtensionService checks broker protocol conformance."""

    def test_is_extension_adapter_for_paper_broker(self) -> None:
        session = boot()
        result = session.extension.is_extension_adapter()
        # PaperBroker may or may not implement ExtensionAdapter
        assert isinstance(result, bool)
        session.stop()


# ---------------------------------------------------------------------------
# StreamSubscription repr
# ---------------------------------------------------------------------------


class TestStreamSubscriptionRepr:
    """StreamSubscription string representation."""

    def test_repr(self) -> None:
        session = boot()
        sub = session.stream.subscribe_quotes(lambda q: None)
        r = repr(sub)
        assert "StreamSubscription" in r
        assert "quotes" in r
        session.stop()
