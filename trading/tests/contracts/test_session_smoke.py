"""End-to-end session smoke tests.

Verify the full boot → session → submit order → events pipeline works
across all execution modes.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from tradex_domain.enums import (
    BrokerId,
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
    TimeInForce,
)
from tradex_domain.events import OrderFilled, OrderPlaced
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_equity() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _make_request(instrument: Any | None = None) -> OrderRequest:
    inst = instrument or _make_equity()
    return OrderRequest(
        instrument=inst,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500.00")),
        time_in_force=TimeInForce.DAY,
        product_type=ProductType.INTRADAY,
    )


# ---------------------------------------------------------------------------
# Smoke: boot() returns a READY session
# ---------------------------------------------------------------------------

class TestBootSmoke:
    """boot() composition root produces a working session."""

    def test_boot_default_returns_session(self) -> None:
        from tradex_trading.runtime.startup import boot
        from tradex_trading.sdk.session import SessionState, TradingSession

        session = boot()
        assert isinstance(session, TradingSession)
        assert session.state == SessionState.READY
        session.stop()

    def test_boot_default_is_paper_mode(self) -> None:
        from tradex_trading.runtime.startup import boot

        session = boot()
        assert session.broker_id == BrokerId.PAPER
        session.stop()

    def test_boot_session_has_all_services(self) -> None:
        from tradex_trading.runtime.startup import boot

        session = boot()
        # Access all services — should not raise
        assert session.broker is not None
        assert session.trade is not None
        assert session.portfolio is not None
        assert session.stream is not None
        assert session.scanner is not None
        session.stop()

    def test_boot_session_has_capabilities(self) -> None:
        from tradex_trading.runtime.startup import boot

        session = boot()
        caps = session.capabilities
        assert caps.supports_market_order is True
        session.stop()

    def test_boot_session_has_bus(self) -> None:
        from tradex_trading.reactive.bus import ReactiveBus
        from tradex_trading.runtime.startup import boot

        session = boot()
        assert isinstance(session.bus, ReactiveBus)
        session.stop()


# ---------------------------------------------------------------------------
# Smoke: submit order through session
# ---------------------------------------------------------------------------

class TestOrderSubmissionSmoke:
    """Full order submission through the SDK session."""

    def test_submit_order_returns_receipt(self) -> None:
        from tradex_domain.execution import OrderReceipt

        from tradex_trading.runtime.startup import boot

        session = boot()
        receipt = session.trade.submit(_make_request())
        assert isinstance(receipt, OrderReceipt)
        assert receipt.status in (OrderStatus.FILLED, OrderStatus.SUBMITTED)
        session.stop()

    def test_submit_order_publishes_events(self) -> None:
        from tradex_trading.runtime.startup import boot

        session = boot()
        events: list[Any] = []
        session.bus.stream().subscribe(lambda m: events.append(m))

        session.trade.submit(_make_request())

        # Should have at least OrderPlaced and OrderFilled
        event_types = [type(e) for e in events]
        assert OrderPlaced in event_types, f"Missing OrderPlaced in {event_types}"
        assert OrderFilled in event_types, f"Missing OrderFilled in {event_types}"
        session.stop()

    def test_submit_multiple_orders(self) -> None:
        from tradex_trading.runtime.startup import boot

        session = boot()
        for i in range(5):
            receipt = session.trade.submit(_make_request())
            assert receipt.status in (OrderStatus.FILLED, OrderStatus.SUBMITTED)
        session.stop()

    def test_broker_returns_quote(self) -> None:
        from tradex_domain.market import Quote

        from tradex_trading.runtime.startup import boot

        session = boot()
        quote = session.broker.get_quote(_make_equity())
        assert isinstance(quote, Quote)
        session.stop()

    def test_broker_returns_ltp(self) -> None:
        from tradex_domain.value_objects import Price

        from tradex_trading.runtime.startup import boot

        session = boot()
        ltp = session.broker.ltp(_make_equity())
        assert isinstance(ltp, Price)
        session.stop()

    def test_portfolio_service_returns_account(self) -> None:
        from tradex_domain.execution import Account

        from tradex_trading.runtime.startup import boot

        session = boot()
        account = session.portfolio.account()
        assert isinstance(account, Account)
        session.stop()


# ---------------------------------------------------------------------------
# Smoke: session lifecycle
# ---------------------------------------------------------------------------

class TestSessionLifecycle:
    """Session lifecycle: NEW → READY → STOPPED."""

    def test_stop_transitions_to_stopped(self) -> None:
        from tradex_trading.runtime.startup import boot
        from tradex_trading.sdk.session import SessionState

        session = boot()
        assert session.state == SessionState.READY
        session.stop()
        assert session.state == SessionState.STOPPED

    def test_services_raise_after_stop(self) -> None:
        from tradex_domain.errors import SessionStateError

        from tradex_trading.runtime.startup import boot

        session = boot()
        session.stop()
        with pytest.raises(SessionStateError):
            _ = session.trade

    def test_double_stop_is_safe(self) -> None:
        from tradex_trading.runtime.startup import boot

        session = boot()
        session.stop()
        session.stop()  # Should not raise


# ---------------------------------------------------------------------------
# Smoke: kill switch
# ---------------------------------------------------------------------------

class TestKillSwitchSmoke:
    """Kill switch blocks order submission."""

    def test_kill_switch_rejects_orders(self) -> None:
        from tradex_trading.runtime.startup import boot

        session = boot()
        session._engine.kill_switch = True

        receipt = session.trade.submit(_make_request())
        assert receipt.status == OrderStatus.REJECTED
        assert "kill_switch" in receipt.message.lower() or "kill" in receipt.message.lower()
        session.stop()

    def test_kill_switch_publishes_no_fill_events(self) -> None:
        from tradex_trading.runtime.startup import boot

        session = boot()
        events: list[Any] = []
        session.bus.stream().subscribe(lambda m: events.append(m))

        session._engine.kill_switch = True
        session.trade.submit(_make_request())

        event_types = [type(e) for e in events]
        assert OrderFilled not in event_types
        session.stop()


# ---------------------------------------------------------------------------
# Smoke: stream service
# ---------------------------------------------------------------------------

class TestStreamServiceSmoke:
    """Stream service subscriptions work."""

    def test_subscribe_quotes(self) -> None:
        from tradex_trading.runtime.startup import boot

        session = boot()
        quotes: list[Any] = []
        sub = session.stream.subscribe_quotes(lambda q: quotes.append(q))
        assert sub is not None
        session.stop()

    def test_subscribe_fills(self) -> None:
        from tradex_trading.runtime.startup import boot

        session = boot()
        fills: list[Any] = []
        sub = session.stream.subscribe_fills(lambda f: fills.append(f))
        assert sub is not None
        session.stop()
