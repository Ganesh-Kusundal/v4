"""Live wiring tests — PaperBroker boot path only.

Ported from v3 ``test_live_wiring.py`` (PaperBroker path only).

v4 API differences:
- Dhan/Upstox brokers are stubs — skip live broker wiring
- PaperBroker boot path works via ``boot()`` with default config
- Kill switch and live order gate are tested in audit regressions
"""

from __future__ import annotations

from decimal import Decimal

from tradex_domain import BrokerId
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, ProductType, TimeInForce
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.config.schema import AppConfig
from tradex_trading.runtime.startup import boot


def _request() -> OrderRequest:
    return OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("1")),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
        product_type=ProductType.INTRADAY,
    )


# ---------------------------------------------------------------------------
# Paper mode wiring
# ---------------------------------------------------------------------------


class TestPaperWiring:
    """PaperBroker boot wiring works correctly."""

    def test_paper_boot_returns_ready_session(self) -> None:
        session = boot(AppConfig(mode="paper"))
        assert session.state == "READY"
        assert session.broker_id == BrokerId.PAPER
        assert session.mode == "paper"
        session.stop()

    def test_paper_session_routes_order_through_engine(self) -> None:
        session = boot(AppConfig(mode="paper"))
        receipt = session.trade.submit(_request())
        assert receipt.status in (OrderStatus.FILLED, OrderStatus.SUBMITTED)
        session.stop()

    def test_paper_session_has_all_services(self) -> None:
        session = boot()
        assert session.broker is not None
        assert session.trade is not None
        assert session.portfolio is not None
        assert session.stream is not None
        assert session.scanner is not None
        session.stop()

    def test_paper_session_broker_returns_quote(self) -> None:
        from tradex_domain.market import Quote

        session = boot()
        quote = session.broker.get_quote(Equity.of("NSE", "RELIANCE"))
        assert isinstance(quote, Quote)
        session.stop()

    def test_paper_session_portfolio_returns_account(self) -> None:
        from tradex_domain.execution import Account

        session = boot()
        account = session.portfolio.account()
        assert isinstance(account, Account)
        session.stop()


# ---------------------------------------------------------------------------
# Live order gate — opt-in required
# ---------------------------------------------------------------------------


class TestLiveOrderGate:
    """Live orders require explicit opt-in via config."""

    def test_live_orders_default_off(self) -> None:
        config = AppConfig(mode="paper")
        # Default config should not allow live orders
        assert config.live_enabled is False

    def test_live_orders_require_explicit_opt_in(self) -> None:
        config = AppConfig(live_enabled=True)
        assert config.live_enabled is True
