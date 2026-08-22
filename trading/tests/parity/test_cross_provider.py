"""Cross-provider parity tests (P1–P4).

Verify that Paper, Dhan, and Upstox adapters all satisfy the BrokerAdapter
protocol and that the same SDK operations work identically across providers.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from tradex_brokers.common.capabilities import (
    dhan_capabilities,
    paper_capabilities,
    upstox_capabilities,
)
from tradex_domain.capabilities import BrokerCapabilities
from tradex_domain.enums import (
    AssetClass,
    BrokerId,
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
    Timeframe,
    TimeInForce,
)
from tradex_domain.execution import (
    Account,
    Order,
    OrderRequest,
    PortfolioSnapshot,
)
from tradex_domain.instruments import Equity, Instrument
from tradex_domain.market import Depth, HistoricalSeries, Quote
from tradex_domain.protocols import BrokerAdapter
from tradex_domain.value_objects import AccountId, OrderId, Price, Quantity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_equity() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _make_order_request(instrument: Instrument | None = None) -> OrderRequest:
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


def _create_broker(broker_id: BrokerId) -> Any:
    """Create a broker adapter instance by ID."""
    from tradex_brokers import DhanBroker, PaperBroker, UpstoxBroker

    return {
        BrokerId.PAPER: PaperBroker,
        BrokerId.DHAN: DhanBroker,
        BrokerId.UPSTOX: UpstoxBroker,
    }[broker_id]()


# ---------------------------------------------------------------------------
# P1: Protocol conformance — all brokers satisfy BrokerAdapter
# ---------------------------------------------------------------------------

class TestProtocolConformance:
    """P1: All broker adapters satisfy the BrokerAdapter protocol."""

    @pytest.mark.parametrize("broker_id", [BrokerId.PAPER, BrokerId.DHAN, BrokerId.UPSTOX])
    def test_satisfies_broker_adapter_protocol(self, broker_id: BrokerId) -> None:
        broker = _create_broker(broker_id)
        assert isinstance(broker, BrokerAdapter), (
            f"{broker_id.value} does not satisfy BrokerAdapter protocol"
        )

    @pytest.mark.parametrize("broker_id", [BrokerId.PAPER, BrokerId.DHAN, BrokerId.UPSTOX])
    def test_has_capabilities(self, broker_id: BrokerId) -> None:
        broker = _create_broker(broker_id)
        assert isinstance(broker.capabilities, BrokerCapabilities)

    @pytest.mark.parametrize("broker_id", [BrokerId.PAPER, BrokerId.DHAN, BrokerId.UPSTOX])
    def test_has_connect_close(self, broker_id: BrokerId) -> None:
        broker = _create_broker(broker_id)
        assert hasattr(broker, "connect")
        assert hasattr(broker, "close")
        assert callable(broker.connect)
        assert callable(broker.close)

    @pytest.mark.parametrize("broker_id", [BrokerId.PAPER, BrokerId.DHAN, BrokerId.UPSTOX])
    def test_has_all_order_methods(self, broker_id: BrokerId) -> None:
        broker = _create_broker(broker_id)
        methods = [
            "submit_order", "cancel_order", "modify_order",
            "get_order", "get_orderbook",
        ]
        for method in methods:
            assert hasattr(broker, method), f"{broker_id.value} missing {method}"

    @pytest.mark.parametrize("broker_id", [BrokerId.PAPER, BrokerId.DHAN, BrokerId.UPSTOX])
    def test_has_all_portfolio_methods(self, broker_id: BrokerId) -> None:
        broker = _create_broker(broker_id)
        for method in ["get_positions", "get_holdings", "get_account", "get_portfolio"]:
            assert hasattr(broker, method), f"{broker_id.value} missing {method}"

    @pytest.mark.parametrize("broker_id", [BrokerId.PAPER, BrokerId.DHAN, BrokerId.UPSTOX])
    def test_has_all_market_data_methods(self, broker_id: BrokerId) -> None:
        broker = _create_broker(broker_id)
        for method in ["get_quote", "ltp", "depth", "history", "search"]:
            assert hasattr(broker, method), f"{broker_id.value} missing {method}"

    @pytest.mark.parametrize("broker_id", [BrokerId.PAPER, BrokerId.DHAN, BrokerId.UPSTOX])
    def test_has_load_instruments(self, broker_id: BrokerId) -> None:
        broker = _create_broker(broker_id)
        assert hasattr(broker, "load_instruments")


# ---------------------------------------------------------------------------
# P2: Capability matrix — each broker reports correct capabilities
# ---------------------------------------------------------------------------

class TestCapabilityMatrix:
    """P2: Each broker reports correct capabilities (fail-closed)."""

    def test_paper_capabilities_are_subset_of_dhan(self) -> None:
        import dataclasses
        paper = paper_capabilities()
        dhan = dhan_capabilities()
        # Every capability paper claims, dhan should also claim (superset)
        for f in dataclasses.fields(paper):
            field_name = f.name
            if field_name == "supported_asset_classes":
                continue
            paper_val = getattr(paper, field_name)
            dhan_val = getattr(dhan, field_name)
            if isinstance(paper_val, bool) and paper_val:
                assert dhan_val is True, (
                    f"Paper claims {field_name}=True but Dhan claims False"
                )

    def test_dhan_supports_super_orders(self) -> None:
        caps = dhan_capabilities()
        assert caps.supports_super_order is True

    def test_upstox_does_not_support_super_orders(self) -> None:
        caps = upstox_capabilities()
        assert caps.supports_super_order is False

    def test_paper_does_not_support_super_orders(self) -> None:
        caps = paper_capabilities()
        assert caps.supports_super_order is False

    def test_dhan_supports_edis(self) -> None:
        caps = dhan_capabilities()
        assert caps.supports_edis is True

    def test_upstox_does_not_support_edis(self) -> None:
        caps = upstox_capabilities()
        assert caps.supports_edis is False

    def test_all_brokers_support_market_orders(self) -> None:
        for factory in [paper_capabilities, dhan_capabilities, upstox_capabilities]:
            caps = factory()
            assert caps.supports_market_order is True

    def test_dhan_supports_all_asset_classes(self) -> None:
        caps = dhan_capabilities()
        assert AssetClass.COMMODITY in caps.supported_asset_classes
        assert AssetClass.CURRENCY in caps.supported_asset_classes

    def test_paper_supported_asset_classes(self) -> None:
        caps = paper_capabilities()
        assert AssetClass.EQUITY in caps.supported_asset_classes


# ---------------------------------------------------------------------------
# P3: Order lifecycle parity — same operations work across Paper broker
# ---------------------------------------------------------------------------

class TestOrderLifecycleParity:
    """P3: Order lifecycle works identically on Paper broker."""

    def test_paper_submit_returns_order_id(self) -> None:
        broker = _create_broker(BrokerId.PAPER)
        broker.connect()
        req = _make_order_request()
        oid = broker.submit_order(req)
        assert isinstance(oid, OrderId)
        assert oid.value.startswith("PAPER-")

    def test_paper_get_order_after_submit(self) -> None:
        broker = _create_broker(BrokerId.PAPER)
        broker.connect()
        req = _make_order_request()
        oid = broker.submit_order(req)
        order = broker.get_order(oid)
        assert isinstance(order, Order)
        assert order.order_id == oid
        assert order.status == OrderStatus.ACK

    def test_paper_cancel_order(self) -> None:
        broker = _create_broker(BrokerId.PAPER)
        broker.connect()
        req = _make_order_request()
        oid = broker.submit_order(req)
        cancelled = broker.cancel_order(oid)
        assert cancelled.status == OrderStatus.CANCELLED

    def test_paper_modify_order(self) -> None:
        broker = _create_broker(BrokerId.PAPER)
        broker.connect()
        req = _make_order_request()
        oid = broker.submit_order(req)
        new_req = _make_order_request()
        # Change quantity
        new_req = OrderRequest(
            instrument=req.instrument,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("20")),
            price=Price(value=Decimal("2550.00")),
        )
        modified = broker.modify_order(oid, new_req)
        assert modified.quantity.value == Decimal("20")

    def test_paper_orderbook_grows(self) -> None:
        broker = _create_broker(BrokerId.PAPER)
        broker.connect()
        assert len(broker.get_orderbook()) == 0
        broker.submit_order(_make_order_request())
        assert len(broker.get_orderbook()) == 1
        broker.submit_order(_make_order_request())
        assert len(broker.get_orderbook()) == 2

    def test_paper_get_account(self) -> None:
        broker = _create_broker(BrokerId.PAPER)
        broker.connect()
        account = broker.get_account()
        assert isinstance(account, Account)
        assert account.account_id == AccountId(value="PAPER")

    def test_paper_get_portfolio(self) -> None:
        broker = _create_broker(BrokerId.PAPER)
        broker.connect()
        portfolio = broker.get_portfolio()
        assert isinstance(portfolio, PortfolioSnapshot)

    def test_paper_get_quote(self) -> None:
        broker = _create_broker(BrokerId.PAPER)
        broker.connect()
        inst = _make_equity()
        quote = broker.get_quote(inst)
        assert isinstance(quote, Quote)
        assert isinstance(quote.ltp, Price)

    def test_paper_ltp(self) -> None:
        broker = _create_broker(BrokerId.PAPER)
        broker.connect()
        inst = _make_equity()
        price = broker.ltp(inst)
        assert isinstance(price, Price)

    def test_paper_depth(self) -> None:
        broker = _create_broker(BrokerId.PAPER)
        broker.connect()
        inst = _make_equity()
        depth = broker.depth(inst)
        assert isinstance(depth, Depth)

    def test_paper_history(self) -> None:
        broker = _create_broker(BrokerId.PAPER)
        broker.connect()
        inst = _make_equity()
        now = datetime.now()
        series = broker.history(inst, Timeframe.D1, now - timedelta(days=30), now)
        assert isinstance(series, HistoricalSeries)


# ---------------------------------------------------------------------------
# Cross-provider: connect/close lifecycle
# ---------------------------------------------------------------------------

class TestLifecycleParity:
    """All brokers can connect and close without error."""

    @pytest.mark.parametrize("broker_id", [BrokerId.PAPER, BrokerId.DHAN, BrokerId.UPSTOX])
    def test_connect_close_lifecycle(self, broker_id: BrokerId) -> None:
        broker = _create_broker(broker_id)
        broker.connect()
        broker.close()
        # No exception = pass

    @pytest.mark.parametrize("broker_id", [BrokerId.PAPER, BrokerId.DHAN, BrokerId.UPSTOX])
    def test_double_connect_is_safe(self, broker_id: BrokerId) -> None:
        broker = _create_broker(broker_id)
        broker.connect()
        broker.connect()  # Should not raise
        broker.close()

    @pytest.mark.parametrize("broker_id", [BrokerId.PAPER, BrokerId.DHAN, BrokerId.UPSTOX])
    def test_load_instruments_is_safe(self, broker_id: BrokerId) -> None:
        broker = _create_broker(broker_id)
        broker.load_instruments()  # Should not raise
