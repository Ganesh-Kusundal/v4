"""End-to-end session smoke tests.

Verify the full boot → session → submit order → events pipeline works
across all execution modes.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

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
from trading.tests.risk_support import app_config

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: AC-10: booting paper requires a named risk policy. This suite submits
#: real orders, so it takes the permissive one — the risk gate stays in the
#: path and is simply far above any notional here.
_CFG = app_config()

def _make_equity() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _make_request(
    instrument: Any | None = None, quantity: str = "10"
) -> OrderRequest:
    inst = instrument or _make_equity()
    return OrderRequest(
        instrument=inst,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(quantity)),
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
        from tradex_runtime.session_impl import SessionState, TradingSession
        from tradex_runtime.startup import boot

        session = boot(_CFG)
        assert isinstance(session, TradingSession)
        assert session.state == SessionState.READY
        session.stop()

    def test_boot_default_is_paper_mode(self) -> None:
        from tradex_runtime.startup import boot

        session = boot(_CFG)
        assert session.broker_id == BrokerId.PAPER
        session.stop()

    def test_boot_session_has_all_services(self) -> None:
        from tradex_runtime.startup import boot

        session = boot(_CFG)
        # Access all services — should not raise
        assert session.broker is not None
        assert session.engine is not None
        assert session.bus is not None
        assert session._scanner_engine is not None
        session.stop()

    def test_boot_session_has_capabilities(self) -> None:
        from tradex_runtime.startup import boot

        session = boot(_CFG)
        caps = session.capabilities
        assert caps.supports_market_order is True
        session.stop()

    def test_boot_session_has_bus(self) -> None:
        from tradex_reactive.bus import ReactiveBus
        from tradex_runtime.startup import boot

        session = boot(_CFG)
        assert isinstance(session.bus, ReactiveBus)
        session.stop()


# ---------------------------------------------------------------------------
# Smoke: submit order through session
# ---------------------------------------------------------------------------

class TestOrderSubmissionSmoke:
    """Full order submission through the SDK session."""

    def test_submit_order_returns_receipt(self) -> None:
        from tradex_domain.execution import OrderReceipt
        from tradex_runtime.startup import boot

        session = boot(_CFG)
        receipt = session.engine.submit(_make_request())
        assert isinstance(receipt, OrderReceipt)
        assert receipt.status in (OrderStatus.FILLED, OrderStatus.SUBMITTED)
        session.stop()

    def test_submit_order_publishes_events(self) -> None:
        from tradex_runtime.startup import boot

        session = boot(_CFG)
        events: list[Any] = []
        session.bus.stream().subscribe(lambda m: events.append(m))

        session.engine.submit(_make_request())

        # Should have at least OrderPlaced and OrderFilled
        event_types = [type(e) for e in events]
        assert OrderPlaced in event_types, f"Missing OrderPlaced in {event_types}"
        assert OrderFilled in event_types, f"Missing OrderFilled in {event_types}"
        session.stop()

    def test_submit_multiple_orders(self) -> None:
        """Five orders, all of which the account can actually pay for.

        At 10 shares of 2,500 this asked for 125,000 of notional against the
        paper broker's 100,000 of starting cash, so the fourth and fifth could
        only ever be unfunded. That went unnoticed while an unfunded order was
        rejected quietly; now the cash ledger raises on it, which is the correct
        behaviour and not something a smoke test should be relying on either way.
        One share per order is 12,500 of notional for the five, fees included,
        well inside the balance — so the test measures that five orders are
        accepted rather than that five orders were attempted.
        """
        from tradex_runtime.startup import boot

        session = boot(_CFG)
        for i in range(5):
            receipt = session.engine.submit(_make_request(quantity="1"))
            assert receipt.status in (OrderStatus.FILLED, OrderStatus.SUBMITTED)
        session.stop()

    def test_broker_returns_quote(self) -> None:
        from tradex_domain.market import Quote
        from tradex_runtime.startup import boot

        session = boot(_CFG)
        quote = session.broker.get_quote(_make_equity())
        assert isinstance(quote, Quote)
        session.stop()

    def test_broker_returns_ltp(self) -> None:
        from tradex_domain.value_objects import Price
        from tradex_runtime.startup import boot

        session = boot(_CFG)
        ltp = session.broker.ltp(_make_equity())
        assert isinstance(ltp, Price)
        session.stop()

    def test_portfolio_service_returns_account(self) -> None:
        from tradex_domain.execution import Account
        from tradex_runtime.startup import boot

        session = boot(_CFG)
        account = session.broker.get_account()
        assert isinstance(account, Account)
        session.stop()


# ---------------------------------------------------------------------------
# Smoke: session lifecycle
# ---------------------------------------------------------------------------

class TestSessionLifecycle:
    """Session lifecycle: NEW → READY → STOPPED."""

    def test_stop_transitions_to_stopped(self) -> None:
        from tradex_runtime.session_impl import SessionState
        from tradex_runtime.startup import boot

        session = boot(_CFG)
        assert session.state == SessionState.READY
        session.stop()
        assert session.state == SessionState.STOPPED

    def test_double_stop_is_safe(self) -> None:
        from tradex_runtime.startup import boot

        session = boot(_CFG)
        session.stop()
        session.stop()  # Should not raise


# ---------------------------------------------------------------------------
# Smoke: kill switch
# ---------------------------------------------------------------------------

class TestKillSwitchSmoke:
    """Kill switch blocks order submission."""

    def test_kill_switch_rejects_orders(self) -> None:
        from tradex_runtime.startup import boot

        session = boot(_CFG)
        session._engine.kill_switch = True

        receipt = session.engine.submit(_make_request())
        assert receipt.status == OrderStatus.REJECTED
        assert "kill_switch" in receipt.message.lower() or "kill" in receipt.message.lower()
        session.stop()

    def test_kill_switch_publishes_no_fill_events(self) -> None:
        from tradex_runtime.startup import boot

        session = boot(_CFG)
        events: list[Any] = []
        session.bus.stream().subscribe(lambda m: events.append(m))

        session._engine.kill_switch = True
        session.engine.submit(_make_request())

        event_types = [type(e) for e in events]
        assert OrderFilled not in event_types
        session.stop()


# ---------------------------------------------------------------------------
# Smoke: bus subscriptions
# ---------------------------------------------------------------------------

class TestBusSubscriptionSmoke:
    """Bus subscription subscriptions work."""

    def test_subscribe_quotes(self) -> None:
        from tradex_domain.market import Quote
        from tradex_runtime.startup import boot

        session = boot(_CFG)
        quotes: list[Any] = []
        sub = session.bus.of_type(Quote).subscribe(lambda q: quotes.append(q))
        assert sub is not None
        session.stop()

    def test_subscribe_fills(self) -> None:
        from tradex_runtime.startup import boot

        session = boot(_CFG)
        fills: list[Any] = []
        sub = session.bus.of_type(OrderFilled).subscribe(lambda f: fills.append(f))
        assert sub is not None
        session.stop()
