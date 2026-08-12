"""Tests for v3-parity service methods ported to v4.

Covers all missing methods listed in the porting spec:
- MarketService: ltp_batch, quote_batch, search, option_chain, future_chain
- TradeService: submit, cancel, modify_order, get_order, get_orderbook,
  _require_order_gate, bind_execution_engine
- PortfolioService: positions, account, portfolio, get_holdings
- StreamService: subscribe_orders, subscribe_positions, unsubscribe, close
- AnalyticsService: indicator, report
- ExtensionService: all extension methods
- ScannerService: run, top
- TradingSession: stop, bind_execution_engine, equity, index, future, option, _require_ready
- Helpers: _broker_capabilities, _as_order_id
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest
from tradex_domain.capabilities import BrokerCapabilities
from tradex_domain.enums import (
    BrokerId,
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
    Timeframe,
    TimeInForce,
)
from tradex_domain.errors import (
    CapabilityNotSupportedError,
    OrderRejectedError,
    SessionStateError,
)
from tradex_domain.execution import (
    Account,
    Order,
    OrderReceipt,
    OrderRequest,
    PortfolioSnapshot,
    Position,
)
from tradex_domain.instruments import Equity, Future, Index, Instrument, Option
from tradex_domain.market import Depth, HistoricalSeries, Quote
from tradex_domain.options import OptionChain
from tradex_domain.strategy import ScannerDefinition
from tradex_domain.value_objects import (
    AccountId,
    InstrumentId,
    OrderId,
    Price,
    Quantity,
)

from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.sdk.session import (
    AnalyticsService,
    EdisStatus,
    ExtensionService,
    KillSwitchResult,
    OrderResult,
    ScannerService,
    SessionState,
    TpinResult,
    TradingSession,
    _as_order_id,
    _broker_capabilities,
)
from tradex_trading.sdk.streaming import StreamSubscription

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_equity() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _make_request(instrument: Instrument | None = None) -> OrderRequest:
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


def _make_session(
    broker: Any = None,
    *,
    live_orders_enabled: bool = True,
    scanner_engine: Any = None,
    stream_backend: Any = None,
    analytics_engine: Any = None,
) -> TradingSession:
    """Create a minimal READY session for testing."""
    from tradex_trading.execution.engine import ExecutionEngine
    from tradex_trading.execution.fill_sources import PaperFillSource
    from tradex_trading.execution.trading_cache import TradingCache

    bus = ReactiveBus()
    cache = TradingCache()
    engine = ExecutionEngine(bus=bus, fill_source=PaperFillSource(), cache=cache)
    mock_broker = broker or _MockBroker()
    session = TradingSession(
        broker=mock_broker,
        bus=bus,
        engine=engine,
        cache=cache,
        broker_id=BrokerId.PAPER,
        mode="paper",
        scanner_engine=scanner_engine,
        stream_backend=stream_backend,
        analytics_engine=analytics_engine,
        live_orders_enabled=live_orders_enabled,
    )
    session.start()
    return session


class _MockBroker:
    """Minimal mock broker with configurable capabilities and method stubs."""

    def __init__(
        self,
        capabilities: BrokerCapabilities | None = None,
    ) -> None:
        self.capabilities = capabilities or BrokerCapabilities(
            supports_market_order=True,
            supports_limit_order=True,
            supports_stop_order=True,
            supports_modify=True,
            supports_cancel=True,
            supports_batch_market_data=True,
            supports_option_chain=True,
            supports_future_chain=True,
            supports_super_order=True,
            supports_forever_order=True,
            supports_slice_order=True,
            supports_edis=True,
            supports_kill_switch=True,
            supports_portfolio_stream=True,
            supports_news=True,
        )
        self._orders: dict[str, Order] = {}
        self._connected = False

    def connect(self) -> None:
        self._connected = True

    def close(self) -> None:
        self._connected = False

    # orders
    def submit_order(self, request: OrderRequest) -> OrderId:
        return OrderId(value="mock-order-1")

    def cancel_order(self, order_id: OrderId) -> Order:
        return self._make_order(order_id, OrderStatus.CANCELLED)

    def modify_order(self, order_id: OrderId, request: OrderRequest) -> Order:
        return self._make_order(order_id, OrderStatus.ACK)

    def get_order(self, order_id: OrderId) -> Order:
        return self._make_order(order_id, OrderStatus.ACK)

    def get_orderbook(self) -> list[Order]:
        return list(self._orders.values())

    # portfolio
    def get_positions(self) -> list[Position]:
        return []

    def get_holdings(self) -> list[Position]:
        return []

    def get_account(self) -> Account:
        return Account(account_id=AccountId(value="mock"))

    def get_portfolio(self) -> PortfolioSnapshot:
        return PortfolioSnapshot()

    # market data
    def get_quote(self, instrument: Instrument) -> Quote:
        return Quote(
            instrument=instrument,
            ltp=Price(value=Decimal("100")),
        )

    def ltp(self, instrument: Instrument) -> Price:
        return Price(value=Decimal("100"))

    def depth(self, instrument: Instrument) -> Depth:
        return Depth(instrument=instrument)

    def history(self, instrument: Any, timeframe: Any, start: Any, end: Any) -> HistoricalSeries:
        return HistoricalSeries(
            instrument=instrument, timeframe=timeframe, candles=[], start=start, end=end,
        )

    def ltp_batch(self, instruments: Any) -> dict[InstrumentId, Price]:
        return {inst.instrument_id: Price(value=Decimal("100")) for inst in instruments}

    def quote_batch(self, instruments: Any) -> dict[InstrumentId, Quote]:
        return {
            inst.instrument_id: Quote(instrument=inst, ltp=Price(value=Decimal("100")))
            for inst in instruments
        }

    def search(self, query: str) -> list[Instrument]:
        return [Equity.of("NSE", query.upper())]

    def get_option_chain(self, underlying: Instrument, expiry: Any = None) -> OptionChain:
        return OptionChain(underlying=underlying)

    def future_chain(self, underlying: Instrument) -> list:
        return []

    def get_news(
        self,
        category: str,
        *,
        instrument_keys: list[str] | None = None,
        page_number: int | None = None,
        page_size: int | None = None,
    ) -> list[dict[str, object]]:
        return [{"heading": "Mock headline", "summary": "Mock summary"}]

    def load_instruments(self) -> None:
        pass

    # extension methods
    def submit_super_order(self, request: OrderRequest) -> OrderId:
        return OrderId(value="super-1")

    def modify_super_order(self, order_id: OrderId, request: OrderRequest) -> OrderResult:
        return OrderResult(order_id=order_id, status=OrderStatus.SUBMITTED)

    def cancel_super_order(self, order_id: OrderId, leg: str = "ENTRY") -> OrderResult:
        return OrderResult(order_id=order_id, status=OrderStatus.CANCELLED)

    def list_super_orders(self) -> list[OrderResult]:
        return [OrderResult(order_id=OrderId(value="super-1"), status=OrderStatus.ACK)]

    def submit_forever_order(self, request: OrderRequest) -> OrderId:
        return OrderId(value="forever-1")

    def modify_forever_order(self, order_id: OrderId, request: OrderRequest) -> OrderResult:
        return OrderResult(order_id=order_id, status=OrderStatus.SUBMITTED)

    def cancel_forever_order(self, order_id: OrderId) -> OrderResult:
        return OrderResult(order_id=order_id, status=OrderStatus.CANCELLED)

    def list_forever_orders(self) -> list[OrderResult]:
        return [OrderResult(order_id=OrderId(value="forever-1"), status=OrderStatus.ACK)]

    def submit_slice_order(
        self, request: OrderRequest, slices: int, interval: timedelta | None = None,
    ) -> list[OrderId]:
        return [OrderId(value=f"slice-{i}") for i in range(slices)]

    def submit_edis(self, request: OrderRequest) -> OrderId:
        return OrderId(value="edis-1")

    def generate_tpin(self) -> dict[str, Any]:
        return {"tpin": "1234"}

    def edis_status(self, isin: str) -> dict[str, Any]:
        return {"isin": isin, "status": "active"}

    def kill_switch(self, enable: bool = True) -> dict[str, Any]:
        return {"kill_switch": enable}

    def status_kill_switch(self) -> dict[str, Any]:
        return {"kill_switch": False}

    # internal
    def _make_order(self, order_id: OrderId, status: OrderStatus) -> Order:
        return Order(
            order_id=order_id,
            instrument=_make_equity(),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("2500")),
            time_in_force=TimeInForce.DAY,
            status=status,
        )


# ===========================================================================
# Helpers
# ===========================================================================


class TestBrokerCapabilities:
    """_broker_capabilities extracts capabilities from a broker."""

    def test_extracts_capabilities(self) -> None:
        broker = _MockBroker()
        caps = _broker_capabilities(broker)
        assert isinstance(caps, BrokerCapabilities)
        assert caps.supports_market_order is True

    def test_returns_empty_for_no_capabilities_attr(self) -> None:
        broker = object()
        caps = _broker_capabilities(broker)
        assert isinstance(caps, BrokerCapabilities)
        assert caps.supports_market_order is False

    def test_returns_empty_for_none_capabilities(self) -> None:
        broker = MagicMock()
        broker.capabilities = None
        caps = _broker_capabilities(broker)
        assert isinstance(caps, BrokerCapabilities)


class TestAsOrderId:
    """_as_order_id coerces values to OrderId."""

    def test_order_id_passthrough(self) -> None:
        oid = OrderId(value="test")
        assert _as_order_id(oid) is oid

    def test_string_coercion(self) -> None:
        result = _as_order_id("test-123")
        assert isinstance(result, OrderId)
        assert result.value == "test-123"

    def test_int_coercion(self) -> None:
        result = _as_order_id(42)
        assert isinstance(result, OrderId)
        assert result.value == "42"


# ===========================================================================
# MarketService
# ===========================================================================


class TestMarketService:
    """MarketService v3-parity methods."""

    def test_ltp_batch(self) -> None:
        session = _make_session()
        instruments = [_make_equity(), Equity.of("NSE", "TCS")]
        result = session.market.ltp_batch(instruments)
        assert isinstance(result, dict)
        assert len(result) == 2
        for v in result.values():
            assert isinstance(v, Price)

    def test_ltp_batch_capability_gate(self) -> None:
        broker = _MockBroker(BrokerCapabilities(supports_batch_market_data=False))
        session = _make_session(broker)
        with pytest.raises(CapabilityNotSupportedError):
            session.market.ltp_batch([_make_equity()])

    def test_quote_batch(self) -> None:
        session = _make_session()
        instruments = [_make_equity()]
        result = session.market.quote_batch(instruments)
        assert isinstance(result, dict)
        for v in result.values():
            assert isinstance(v, Quote)

    def test_quote_batch_capability_gate(self) -> None:
        broker = _MockBroker(BrokerCapabilities(supports_batch_market_data=False))
        session = _make_session(broker)
        with pytest.raises(CapabilityNotSupportedError):
            session.market.quote_batch([_make_equity()])

    def test_depth_nse(self) -> None:
        session = _make_session()
        result = session.market.depth(_make_equity())
        assert isinstance(result, Depth)

    @pytest.mark.parametrize(
        "instrument",
        [
            Equity.of("MCX", "CRUDEOIL"),
            Equity.of("BSE", "RELIANCE"),
            Index.of("IDX", "NIFTY"),
        ],
    )
    def test_depth_gates_non_nse(self, instrument) -> None:
        session = _make_session()
        with pytest.raises(CapabilityNotSupportedError, match="NSE"):
            session.market.depth(instrument)

    def test_search(self) -> None:
        session = _make_session()
        results = session.market.search("RELIANCE")
        assert isinstance(results, list)
        assert len(results) == 1
        assert isinstance(results[0], Instrument)

    def test_option_chain(self) -> None:
        session = _make_session()
        result = session.market.option_chain(_make_equity())
        assert isinstance(result, OptionChain)

    def test_option_chain_with_expiry(self) -> None:
        session = _make_session()
        result = session.market.option_chain(_make_equity(), expiry="2026-12-25")
        assert isinstance(result, OptionChain)

    def test_option_chain_capability_gate(self) -> None:
        broker = _MockBroker(BrokerCapabilities(supports_option_chain=False))
        session = _make_session(broker)
        with pytest.raises(CapabilityNotSupportedError):
            session.market.option_chain(_make_equity())

    def test_future_chain(self) -> None:
        session = _make_session()
        result = session.market.future_chain(_make_equity())
        assert isinstance(result, list)

    def test_future_chain_capability_gate(self) -> None:
        broker = _MockBroker(BrokerCapabilities(supports_future_chain=False))
        session = _make_session(broker)
        with pytest.raises(CapabilityNotSupportedError):
            session.market.future_chain(_make_equity())

    def test_history_canonical(self) -> None:
        session = _make_session()
        start = datetime(2026, 8, 1, tzinfo=UTC)
        end = datetime(2026, 8, 5, tzinfo=UTC)
        series = session.market.history(_make_equity(), Timeframe.D1, start, end)
        assert isinstance(series, HistoricalSeries)
        assert series.start == start
        assert series.end == end

    def test_history_convenience_interval_lookback(self) -> None:
        """interval/lookback_days compute the window ending now."""
        session = _make_session()
        series = session.market.history(_make_equity(), interval="5m", lookback_days=5)
        assert isinstance(series, HistoricalSeries)
        assert series.timeframe == Timeframe.M5
        assert series.end - series.start == timedelta(days=5)

    def test_history_default_lookback(self) -> None:
        """No window → 30-day lookback (repairs the HTTP route's 2-arg call)."""
        session = _make_session()
        series = session.market.history(_make_equity(), Timeframe.D1)
        assert series.end - series.start == timedelta(days=30)

    def test_history_requires_timeframe(self) -> None:
        session = _make_session()
        with pytest.raises(ValueError, match="timeframe or interval"):
            session.market.history(_make_equity())

    def test_news(self) -> None:
        session = _make_session()
        result = session.market.news("instrument_keys", instrument_keys=["NSE_EQ|TEST"])
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["heading"] == "Mock headline"

    def test_news_capability_gate(self) -> None:
        broker = _MockBroker(BrokerCapabilities(supports_news=False))
        session = _make_session(broker)
        with pytest.raises(CapabilityNotSupportedError):
            session.market.news("instrument_keys")


# ===========================================================================
# TradeService
# ===========================================================================


class TestTradeService:
    """TradeService order methods."""

    def test_submit(self) -> None:
        session = _make_session()
        receipt = session.trade.submit(_make_request())
        assert isinstance(receipt, OrderReceipt)

    def test_submit_capability_gate(self) -> None:
        broker = _MockBroker(BrokerCapabilities(supports_limit_order=False))
        session = _make_session(broker)
        with pytest.raises(CapabilityNotSupportedError):
            session.trade.submit(_make_request())

    def test_submit_gate_disabled(self) -> None:
        session = _make_session(live_orders_enabled=False)
        with pytest.raises(OrderRejectedError):
            session.trade.submit(_make_request())

    def test_cancel(self) -> None:
        session = _make_session()
        order = Order(
            order_id=OrderId(value="test-1"),
            instrument=_make_equity(),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("2500.00")),
            time_in_force=TimeInForce.DAY,
            status=OrderStatus.ACK,
        )
        session._cache.update_order(order)  # type: ignore[attr-defined]
        result = session.trade.cancel(OrderId(value="test-1"))
        assert isinstance(result, Order)
        assert result.status == OrderStatus.CANCELLED

    def test_modify_order(self) -> None:
        session = _make_session()
        oid = OrderId(value="test-1")
        result = session.trade.modify_order(oid, _make_request())
        assert isinstance(result, Order)

    def test_modify_order_capability_gate(self) -> None:
        broker = _MockBroker(BrokerCapabilities(supports_modify=False))
        session = _make_session(broker)
        with pytest.raises(CapabilityNotSupportedError):
            session.trade.modify_order(OrderId(value="test"), _make_request())

    def test_get_order(self) -> None:
        session = _make_session()
        result = session.trade.get_order(OrderId(value="test-1"))
        assert isinstance(result, Order)

    def test_get_orderbook(self) -> None:
        session = _make_session()
        result = session.trade.get_orderbook()
        assert isinstance(result, list)

    def test_bind_execution_engine(self) -> None:
        session = _make_session()
        new_engine = MagicMock()
        new_engine.submit.return_value = OrderReceipt(
            order_id=OrderId(value="new-1"),
            status=OrderStatus.SUBMITTED,
        )
        # Access the same service instance (property creates new each time)
        trade = session.trade
        trade.bind_execution_engine(new_engine)
        receipt = trade.submit(_make_request())
        assert receipt.order_id.value == "new-1"

    def test_require_order_gate(self) -> None:
        session = _make_session(live_orders_enabled=False)
        with pytest.raises(OrderRejectedError):
            session.trade._require_order_gate()


# ===========================================================================
# PortfolioService
# ===========================================================================


class TestPortfolioService:
    """PortfolioService methods."""

    def test_get_holdings(self) -> None:
        session = _make_session()
        result = session.portfolio.get_holdings()
        assert isinstance(result, list)

    def test_portfolio(self) -> None:
        session = _make_session()
        result = session.portfolio.portfolio()
        assert isinstance(result, PortfolioSnapshot)

    def test_positions_v4_name(self) -> None:
        session = _make_session()
        result = session.portfolio.positions()
        assert isinstance(result, list)

    def test_account_v4_name(self) -> None:
        session = _make_session()
        result = session.portfolio.account()
        assert isinstance(result, Account)

    def test_holdings_v4_name(self) -> None:
        session = _make_session()
        result = session.portfolio.holdings()
        assert isinstance(result, list)

    def test_funds_falls_back_to_account(self) -> None:
        """Brokers without fund_limits derive available cash from the account."""
        session = _make_session()
        result = session.portfolio.funds()
        assert "available_cash" in result

    def test_funds_delegates_to_fund_limits(self) -> None:
        broker = _MockBroker()
        broker.fund_limits = lambda: {"available": {"total": 50000}}  # type: ignore[attr-defined]
        session = _make_session(broker)
        assert session.portfolio.funds() == {"available": {"total": 50000}}


# ===========================================================================
# StreamService
# ===========================================================================


class TestStreamService:
    """StreamService v3-parity methods."""

    def test_subscribe_orders_with_backend(self) -> None:
        backend = MagicMock()
        mock_sub = MagicMock()
        backend.subscribe_orders.return_value = mock_sub
        session = _make_session(stream_backend=backend)
        session.stream.subscribe_orders(lambda o: None)
        backend.subscribe_orders.assert_called_once()

    def test_subscribe_orders_fallback_to_bus(self) -> None:
        session = _make_session()  # no backend
        sub = session.stream.subscribe_orders(lambda o: None)
        assert isinstance(sub, StreamSubscription)

    def test_subscribe_positions_with_backend(self) -> None:
        backend = MagicMock()
        mock_sub = MagicMock()
        backend.subscribe_positions.return_value = mock_sub
        session = _make_session(stream_backend=backend)
        session.stream.subscribe_positions(lambda p: None)
        backend.subscribe_positions.assert_called_once()

    def test_subscribe_positions_capability_gate(self) -> None:
        broker = _MockBroker(BrokerCapabilities(supports_portfolio_stream=False))
        session = _make_session(broker)
        with pytest.raises(CapabilityNotSupportedError):
            session.stream.subscribe_positions(lambda p: None)

    def test_unsubscribe(self) -> None:
        session = _make_session()
        sub = session.stream.subscribe_quotes(lambda q: None)
        assert sub.is_active
        session.stream.unsubscribe(sub)
        assert not sub.is_active

    def test_close(self) -> None:
        backend = MagicMock()
        session = _make_session(stream_backend=backend)
        session.stream.close()
        backend.close.assert_called_once()

    def test_close_no_backend(self) -> None:
        session = _make_session()
        session.stream.close()  # Should not raise

    def test_backend_subscription_cancel_routes_to_backend(self) -> None:
        """Backend subs return string ids; cancel must call backend.unsubscribe."""

        class FakeBackend:
            def __init__(self) -> None:
                self.unsubscribed: list[str] = []

            def subscribe_orders(self, handler: Any) -> str:
                return "order-1"

            def subscribe_positions(self, handler: Any) -> str:
                return "pos-1"

            def unsubscribe(self, subscription: str) -> None:
                self.unsubscribed.append(subscription)

        backend = FakeBackend()
        session = _make_session(stream_backend=backend)
        sub = session.stream.subscribe_orders(lambda o: None)
        assert sub.is_active
        session.stream.unsubscribe(sub)
        assert backend.unsubscribed == ["order-1"]
        assert not sub.is_active
        # Idempotent — second cancel is a no-op.
        session.stream.unsubscribe(sub)
        assert backend.unsubscribed == ["order-1"]

    def test_session_stop_cancels_backend_subscriptions(self) -> None:
        """stop() iterates and cancels every handle — string-id subs must not crash."""

        class FakeBackend:
            def __init__(self) -> None:
                self.unsubscribed: list[str] = []
                self.closed = False

            def subscribe_orders(self, handler: Any) -> str:
                return "order-1"

            def unsubscribe(self, subscription: str) -> None:
                self.unsubscribed.append(subscription)

            def close(self) -> None:
                self.closed = True

        backend = FakeBackend()
        session = _make_session(stream_backend=backend)
        session.stream.subscribe_orders(lambda o: None)
        session.stop()
        assert backend.unsubscribed == ["order-1"]
        assert backend.closed is True


# ===========================================================================
# AnalyticsService
# ===========================================================================


class TestAnalyticsService:
    """AnalyticsService v3-parity methods."""

    def test_indicator_with_engine(self) -> None:
        engine = MagicMock()
        series = MagicMock()
        engine.indicator.return_value = series
        svc = AnalyticsService(engine)
        result = svc.indicator(series, "sma", period=14)
        engine.indicator.assert_called_once_with(series, "sma", period=14)
        assert result is series

    def test_indicator_without_engine_raises(self) -> None:
        svc = AnalyticsService(None)
        series = MagicMock()
        with pytest.raises(CapabilityNotSupportedError):
            svc.indicator(series, "sma")

    def test_report_with_engine(self) -> None:
        engine = MagicMock()
        expected = {"sharpe": 1.5}
        engine.report.return_value = expected
        svc = AnalyticsService(engine)
        series = MagicMock()
        result = svc.report("performance", series)
        engine.report.assert_called_once()
        assert result == expected

    def test_report_without_engine_raises(self) -> None:
        svc = AnalyticsService(None)
        with pytest.raises(CapabilityNotSupportedError):
            svc.report("performance", MagicMock())

    def test_indicators_stub_without_engine(self) -> None:
        svc = AnalyticsService(None)
        data = [1.0, 2.0]
        with pytest.raises(CapabilityNotSupportedError):
            svc.indicators(data, ["sma"])


# ===========================================================================
# ExtensionService
# ===========================================================================


class TestExtensionService:
    """ExtensionService v3-parity methods."""

    def test_super_order(self) -> None:
        session = _make_session()
        receipt = session.extension.super_order(_make_request())
        assert isinstance(receipt, OrderResult)
        assert receipt.status == "SUBMITTED"

    def test_super_order_capability_gate(self) -> None:
        broker = _MockBroker(BrokerCapabilities(supports_super_order=False))
        session = _make_session(broker)
        with pytest.raises(CapabilityNotSupportedError):
            session.extension.super_order(_make_request())

    def test_modify_super(self) -> None:
        session = _make_session()
        result = session.extension.modify_super(OrderId(value="super-1"), _make_request())
        assert isinstance(result, OrderResult)
        assert result.status == OrderStatus.SUBMITTED

    def test_cancel_super(self) -> None:
        session = _make_session()
        result = session.extension.cancel_super(OrderId(value="super-1"))
        assert isinstance(result, OrderResult)
        assert result.status == OrderStatus.CANCELLED

    def test_list_super(self) -> None:
        session = _make_session()
        result = session.extension.list_super()
        assert isinstance(result, list)
        assert len(result) == 1

    def test_forever_order(self) -> None:
        session = _make_session()
        receipt = session.extension.forever_order(_make_request())
        assert isinstance(receipt, OrderResult)
        assert receipt.status == "SUBMITTED"

    def test_modify_forever(self) -> None:
        session = _make_session()
        result = session.extension.modify_forever(OrderId(value="forever-1"), _make_request())
        assert isinstance(result, OrderResult)
        assert result.status == OrderStatus.SUBMITTED

    def test_cancel_forever(self) -> None:
        session = _make_session()
        result = session.extension.cancel_forever(OrderId(value="forever-1"))
        assert isinstance(result, OrderResult)
        assert result.status == OrderStatus.CANCELLED

    def test_get_forever(self) -> None:
        session = _make_session()
        result = session.extension.get_forever()
        assert isinstance(result, list)

    def test_slice_order(self) -> None:
        session = _make_session()
        receipts = session.extension.slice_order(_make_request(), slices=3)
        assert len(receipts) == 3
        assert all(isinstance(r, OrderResult) for r in receipts)

    def test_edis(self) -> None:
        session = _make_session()
        receipt = session.extension.edis(_make_request())
        assert isinstance(receipt, EdisStatus)

    def test_generate_tpin(self) -> None:
        session = _make_session()
        result = session.extension.generate_tpin()
        assert isinstance(result, TpinResult)

    def test_edis_status(self) -> None:
        session = _make_session()
        result = session.extension.edis_status("INE002A01018")
        assert isinstance(result, EdisStatus)
        assert result.status == "active"

    def test_kill_switch(self) -> None:
        session = _make_session()
        result = session.extension.kill_switch(True)
        assert isinstance(result, KillSwitchResult)

    def test_status_kill_switch(self) -> None:
        session = _make_session()
        result = session.extension.status_kill_switch()
        assert isinstance(result, KillSwitchResult)

    def test_order_gate_enforced(self) -> None:
        session = _make_session(live_orders_enabled=False)
        with pytest.raises(OrderRejectedError):
            session.extension.super_order(_make_request())

    def test_is_extension_adapter(self) -> None:
        session = _make_session()
        result = session.extension.is_extension_adapter()
        assert isinstance(result, bool)

    def test_receipt_helper(self) -> None:
        receipt = ExtensionService._receipt(OrderId(value="test"))
        assert isinstance(receipt, OrderReceipt)
        assert receipt.status == OrderStatus.SUBMITTED


# ===========================================================================
# ScannerService
# ===========================================================================


class TestScannerService:
    """ScannerService v3-parity methods."""

    def test_run_with_engine(self) -> None:
        engine = MagicMock()
        engine.run.return_value = [MagicMock()]
        svc = ScannerService(engine)
        defn = ScannerDefinition()
        result = svc.run(defn)
        engine.run.assert_called_once_with(defn)
        assert len(result) == 1

    def test_run_without_engine_raises(self) -> None:
        svc = ScannerService(None)
        with pytest.raises(CapabilityNotSupportedError):
            svc.run(ScannerDefinition())

    def test_top_with_engine(self) -> None:
        engine = MagicMock()
        engine.top.return_value = [MagicMock(), MagicMock()]
        svc = ScannerService(engine)
        defn = ScannerDefinition()
        result = svc.top(defn, limit=5)
        engine.top.assert_called_once_with(defn, 5)
        assert len(result) == 2

    def test_top_without_engine_raises(self) -> None:
        svc = ScannerService(None)
        with pytest.raises(CapabilityNotSupportedError):
            svc.top(ScannerDefinition())


# ===========================================================================
# TradingSession — lifecycle and instrument factories
# ===========================================================================


class TestTradingSessionLifecycle:
    """TradingSession v3-parity lifecycle and factory methods."""

    def test_stop_transitions_to_stopped(self) -> None:
        session = _make_session()
        assert session.state == SessionState.READY
        session.stop()
        assert session.state == SessionState.STOPPED

    def test_stop_is_idempotent(self) -> None:
        session = _make_session()
        session.stop()
        session.stop()  # Should not raise
        assert session.state == SessionState.STOPPED

    def test_bind_execution_engine(self) -> None:
        session = _make_session()
        new_engine = MagicMock()
        session.bind_execution_engine(new_engine)
        assert session._engine is new_engine

    def test_equity_factory(self) -> None:
        session = _make_session()
        eq = session.equity("NSE", "RELIANCE")
        assert isinstance(eq, Equity)
        assert eq.symbol == "RELIANCE"

    def test_index_factory(self) -> None:
        session = _make_session()
        idx = session.index("IDX", "NIFTY")
        assert isinstance(idx, Index)
        assert idx.symbol == "NIFTY"

    def test_future_factory(self) -> None:
        session = _make_session()
        fut = session.future("NSE", "NIFTY", date(2026, 12, 25))
        assert isinstance(fut, Future)
        assert fut.expiry == date(2026, 12, 25)

    def test_option_factory(self) -> None:
        session = _make_session()
        opt = session.option("NSE", "NIFTY", date(2026, 12, 25), 20000.0, "CE")
        assert isinstance(opt, Option)
        assert opt.right == "CE"
        assert opt.strike == Decimal("20000")

    def test_option_factory_with_price(self) -> None:
        session = _make_session()
        strike = Price(value=Decimal("25000"))
        opt = session.option("NSE", "NIFTY", date(2026, 12, 25), strike, "PE")
        assert isinstance(opt, Option)
        assert opt.right == "PE"
        assert opt.strike == Decimal("25000")

    def test_services_raise_after_stop(self) -> None:
        session = _make_session()
        session.stop()
        with pytest.raises(SessionStateError):
            _ = session.market
        with pytest.raises(SessionStateError):
            _ = session.trade

    def test_account_alias(self) -> None:
        """session.account is the portfolio facade (positions/funds/holdings)."""
        session = _make_session()
        assert session.account is session.portfolio
        assert isinstance(session.account.positions(), list)
        assert isinstance(session.account.holdings(), list)
        assert "available_cash" in session.account.funds()

    def test_instrument_factories_work_in_any_state(self) -> None:
        """Instrument factories are pure value constructors (D-1)."""
        from tradex_trading.execution.engine import ExecutionEngine
        from tradex_trading.execution.fill_sources import PaperFillSource
        from tradex_trading.execution.trading_cache import TradingCache

        bus = ReactiveBus()
        engine = ExecutionEngine(bus=bus, fill_source=PaperFillSource(), cache=TradingCache())
        session = TradingSession(
            broker=_MockBroker(),
            bus=bus,
            engine=engine,
            cache=TradingCache(),
            broker_id=BrokerId.PAPER,
        )
        # Session is NEW — factories should still work
        eq = session.equity("NSE", "TCS")
        assert isinstance(eq, Equity)
        idx = session.index("IDX", "NIFTY")
        assert isinstance(idx, Index)
