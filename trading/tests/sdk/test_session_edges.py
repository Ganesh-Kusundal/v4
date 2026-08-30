"""SDK session edge case tests.

Ported from v3 ``test_sdk_session_edges.py``.

v4 API differences:
- ``TradingSession`` requires ``broker, bus, engine, cache, broker_id``
- Service layer removed; consumers use session.bus / session.engine directly
"""

from __future__ import annotations

import pytest
from tradex_domain.errors import CapabilityNotSupportedError

from tradex_trading.runtime.startup import boot
from tradex_trading.sdk.session import SessionState
from tradex_trading.sdk.streaming import StreamSubscription

# ---------------------------------------------------------------------------
# Session state edges
# ---------------------------------------------------------------------------


class TestSessionStateEdges:
    """Session lifecycle edge cases."""

    def test_state_property_reflects_lifecycle(self) -> None:
        session = boot()
        assert session.state == SessionState.READY
        session.stop()
        assert session.state == SessionState.STOPPED


# ---------------------------------------------------------------------------
# Reactive bus — subscription lifecycle (replaces StreamService tests)
# ---------------------------------------------------------------------------


class TestBusSubscriptionEdges:
    """Reactive bus subscription management."""

    def test_subscribe_quotes_returns_disposable(self) -> None:
        from tradex_domain.market import Quote

        session = boot()
        received: list[Quote] = []
        d = session.bus.of_type(Quote).subscribe(received.append)
        assert not d.is_disposed
        d.dispose()
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
        d = session.bus.of_type(Depth).subscribe(received.append)
        depth = Depth(
            instrument=Equity.of("NSE", "RELIANCE"),
            bids=((Price(value=Decimal("1319.0")), Quantity(value=Decimal("10"))),),
            asks=((Price(value=Decimal("1319.5")), Quantity(value=Decimal("5"))),),
        )
        session.bus.publish(depth)
        assert len(received) == 1
        assert received[0].instrument.symbol == "RELIANCE"
        d.dispose()
        session.stop()

    def test_stop_disposes_subscriptions(self) -> None:
        from tradex_domain.market import Quote

        session = boot()
        sub = StreamSubscription(session.bus.of_type(Quote).subscribe(lambda q: None), "quotes")
        session._subscriptions.append(sub)
        assert sub.is_active
        session.stop()
        # After stop, subscription should be cancelled
        assert not sub.is_active


# ---------------------------------------------------------------------------
# StreamSubscription repr
# ---------------------------------------------------------------------------


class TestStreamSubscriptionRepr:
    """StreamSubscription string representation."""

    def test_repr(self) -> None:
        from tradex_domain.market import Quote

        session = boot()
        sub = StreamSubscription(session.bus.of_type(Quote).subscribe(lambda q: None), "quotes")
        r = repr(sub)
        assert "StreamSubscription" in r
        assert "quotes" in r
        sub.cancel()
        session.stop()
