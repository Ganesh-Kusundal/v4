"""Tests for the v4 Dhan broker adapter (tradex_brokers.dhan.adapter)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from tradex_domain.capabilities import BrokerCapabilities
from tradex_domain.enums import (
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
    Timeframe,
    TimeInForce,
)
from tradex_domain.errors import (
    BrokerUnavailableError,
    CapabilityNotSupportedError,
    OrderRejectedError,
)
from tradex_domain.execution import (
    Account,
    Order,
    OrderRequest,
    PortfolioSnapshot,
)
from tradex_domain.instruments import Equity, Index, Instrument
from tradex_domain.market import Depth, HistoricalSeries, Quote
from tradex_domain.options import OptionChain
from tradex_domain.value_objects import (
    AccountId,
    InstrumentId,
    Money,
    OrderId,
    Price,
    Quantity,
)
from tradex_domain.wire import InstrumentRegistry

from tradex_brokers.dhan.adapter import DhanBroker
from tradex_brokers.dhan.client import DhanApiClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_registry() -> InstrumentRegistry:
    reg = InstrumentRegistry()
    iid = InstrumentId.equity("NSE", "RELIANCE")
    reg.register(iid, {"key": "2885", "asset_class": "EQUITY"})
    reg.add_alias("2885", iid)
    reg.add_alias("RELIANCE", iid)
    idx_id = InstrumentId.equity("IDX", "NIFTY")
    reg.register(idx_id, {"key": "13", "asset_class": "INDEX"})
    reg.add_alias("13", idx_id)
    reg.add_alias("NIFTY", idx_id)
    return reg


def _equity() -> Instrument:
    return Equity.of("NSE", "RELIANCE")


def _index() -> Instrument:
    return Index.of("IDX", "NIFTY")


def _make_broker() -> tuple[DhanBroker, MagicMock]:
    """Build a connected adapter with a mocked DhanApiClient transport."""
    transport = MagicMock(spec=DhanApiClient)
    registry = _make_registry()
    broker = DhanBroker(transport=transport, registry=registry)
    broker.connect()
    return broker, transport


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_connect_without_transport_is_noop(self):
        broker = DhanBroker()
        broker.connect()  # Should not raise — no-op without transport

    def test_connect_with_transport(self):
        transport = MagicMock(spec=DhanApiClient)
        broker = DhanBroker(transport=transport)
        broker.connect()
        # Should not raise

    def test_close(self):
        broker, _ = _make_broker()
        broker.close()
        # After close, calls should fail
        with pytest.raises(BrokerUnavailableError):
            broker.get_orderbook()

    def test_verify_connection_success(self):
        broker, transport = _make_broker()
        transport.get_account.return_value = Account(
            account_id=AccountId(value="dhan"),
            balance=Money(amount=Decimal("50000"), currency="INR"),
        )
        assert broker.verify_connection() is True

    def test_verify_connection_failure(self):
        broker, transport = _make_broker()
        transport.get_account.side_effect = Exception("auth failed")
        assert broker.verify_connection() is False

    def test_capabilities(self):
        broker, _ = _make_broker()
        caps = broker.capabilities
        assert isinstance(caps, BrokerCapabilities)
        assert caps.supports_super_order is True
        assert caps.supports_forever_order is True
        assert caps.supports_kill_switch is True


# ---------------------------------------------------------------------------
# Mutation gate tests
# ---------------------------------------------------------------------------


class TestMutationGate:
    def test_submit_order_when_disabled(self):
        transport = MagicMock(spec=DhanApiClient)
        broker = DhanBroker(transport=transport, allow_order_operations=False)
        broker.connect()
        with pytest.raises(OrderRejectedError):
            broker.submit_order(MagicMock())

    def test_set_order_operations_enabled(self):
        broker, _ = _make_broker()
        broker.set_order_operations_enabled(False)
        with pytest.raises(OrderRejectedError):
            broker.submit_order(MagicMock())
        broker.set_order_operations_enabled(True)
        # Should now delegate
        broker._transport.submit_order.return_value = OrderId(value="1")
        request = OrderRequest(
            instrument=_equity(),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(value=Decimal("10")),
        )
        result = broker.submit_order(request)
        assert result.value == "1"


# ---------------------------------------------------------------------------
# Order delegation tests
# ---------------------------------------------------------------------------


class TestOrderDelegation:
    def test_submit_order(self):
        broker, transport = _make_broker()
        transport.submit_order.return_value = OrderId(value="123")
        request = OrderRequest(
            instrument=_equity(),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(value=Decimal("10")),
        )
        result = broker.submit_order(request)
        assert result.value == "123"
        transport.submit_order.assert_called_once_with(request)

    def test_cancel_order(self):
        broker, transport = _make_broker()
        order = Order(
            order_id=OrderId(value="99"),
            instrument=_equity(),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(value=Decimal("10")),
            price=None,
            time_in_force=TimeInForce.DAY,
            status=OrderStatus.CANCELLED,
        )
        transport.cancel_order.return_value = order
        result = broker.cancel_order(OrderId(value="99"))
        assert result.status == OrderStatus.CANCELLED

    def test_modify_order(self):
        broker, transport = _make_broker()
        order = Order(
            order_id=OrderId(value="99"),
            instrument=_equity(),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("100")),
            time_in_force=TimeInForce.DAY,
            status=OrderStatus.PENDING,
        )
        transport.modify_order.return_value = order
        request = OrderRequest(
            instrument=_equity(),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("100")),
        )
        result = broker.modify_order(OrderId(value="99"), request)
        assert result.order_type == OrderType.LIMIT

    def test_get_order(self):
        broker, transport = _make_broker()
        transport.get_order.return_value = Order(
            order_id=OrderId(value="42"),
            instrument=_equity(),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(value=Decimal("10")),
            price=None,
            time_in_force=TimeInForce.DAY,
            status=OrderStatus.FILLED,
        )
        result = broker.get_order(OrderId(value="42"))
        assert result.status == OrderStatus.FILLED

    def test_get_orderbook(self):
        broker, transport = _make_broker()
        transport.get_orderbook.return_value = []
        assert broker.get_orderbook() == []

    def test_get_order_list(self):
        broker, transport = _make_broker()
        transport.get_order_list.return_value = []
        assert broker.get_order_list() == []

    def test_get_order_by_correlation_id(self):
        broker, transport = _make_broker()
        transport.get_order_by_correlation_id.return_value = {"orderId": "1"}
        result = broker.get_order_by_correlation_id("abc")
        assert result["orderId"] == "1"


# ---------------------------------------------------------------------------
# Portfolio delegation tests
# ---------------------------------------------------------------------------


class TestPortfolioDelegation:
    def test_get_positions(self):
        broker, transport = _make_broker()
        transport.get_positions.return_value = []
        assert broker.get_positions() == []

    def test_get_holdings(self):
        broker, transport = _make_broker()
        transport.get_holdings.return_value = []
        assert broker.get_holdings() == []

    def test_get_account(self):
        broker, transport = _make_broker()
        snapshot = Account(
            account_id=AccountId(value="dhan"),
            balance=Money(amount=Decimal("50000"), currency="INR"),
        )
        transport.get_account.return_value = snapshot
        result = broker.get_account()
        assert result.balance.amount == Decimal("50000")

    def test_get_portfolio(self):
        broker, transport = _make_broker()
        transport.get_portfolio.return_value = PortfolioSnapshot()
        result = broker.get_portfolio()
        assert isinstance(result, PortfolioSnapshot)

    def test_convert_position(self):
        broker, transport = _make_broker()
        transport.convert_position.return_value = {"status": "success"}
        result = broker.convert_position(
            _equity(),
            from_product=ProductType.INTRADAY,
            to_product=ProductType.DELIVERY,
            quantity=10,
        )
        assert result["status"] == "success"

    def test_exit_all(self):
        broker, transport = _make_broker()
        transport.exit_all.return_value = {"status": "ok"}
        result = broker.exit_all()
        assert result["status"] == "ok"

    def test_mass_status(self):
        broker, transport = _make_broker()
        transport.mass_status.return_value = {"orders": [], "positions": []}
        result = broker.mass_status()
        assert "orders" in result


# ---------------------------------------------------------------------------
# Market data delegation tests
# ---------------------------------------------------------------------------


class TestMarketDataDelegation:
    def test_get_quote(self):
        broker, transport = _make_broker()
        eq = _equity()
        transport.get_quote.return_value = Quote(
            instrument=eq,
            ltp=Price(value=Decimal("2500")),
        )
        result = broker.get_quote(eq)
        assert result.ltp.value == Decimal("2500")

    @pytest.mark.parametrize(
        "instrument",
        [
            Equity.of("MCX", "CRUDEOIL"),
            Equity.of("BSE", "RELIANCE"),
            Index.of("IDX", "NIFTY"),
        ],
    )
    def test_depth_gates_non_nse(self, instrument):
        broker, _ = _make_broker()
        with pytest.raises(CapabilityNotSupportedError, match="NSE"):
            broker.depth(instrument)

    def test_ltp(self):
        broker, transport = _make_broker()
        transport.ltp.return_value = Price(value=Decimal("2500"))
        result = broker.ltp(_equity())
        assert result.value == Decimal("2500")

    def test_depth(self):
        broker, transport = _make_broker()
        eq = _equity()
        transport.depth.return_value = Depth(instrument=eq)
        result = broker.depth(eq)
        assert isinstance(result, Depth)

    def test_ltp_batch(self):
        broker, transport = _make_broker()
        transport.ltp_batch.return_value = {}
        result = broker.ltp_batch([_equity()])
        assert result == {}

    def test_quote_batch(self):
        broker, transport = _make_broker()
        transport.quote_batch.return_value = {}
        result = broker.quote_batch([_equity()])
        assert result == {}

    def test_history(self):
        broker, transport = _make_broker()
        eq = _equity()
        start = datetime(2026, 8, 1, tzinfo=UTC)
        end = datetime(2026, 8, 5, tzinfo=UTC)
        transport.history.return_value = HistoricalSeries(
            instrument=eq, timeframe=Timeframe.D1, candles=[], start=start, end=end
        )
        result = broker.history(eq, Timeframe.D1, start, end)
        assert isinstance(result, HistoricalSeries)

    def test_get_option_chain(self):
        broker, transport = _make_broker()
        eq = _equity()
        transport.get_option_chain.return_value = OptionChain(underlying=eq)
        result = broker.get_option_chain(eq)
        assert isinstance(result, OptionChain)

    def test_get_option_chain_mcx_uses_rest(self):
        """MCX chains come from the REST endpoint, not the master.

        Regression: the adapter used to gate every non-NFO/BFO/IDX exchange to
        the instrument master, but Dhan's /optionchain serves MCX (and
        NSE_COMM/CDS/BCD) via ``UnderlyingSeg`` + a scrip id. Tradehull parity:
        the ``UnderlyingScrip`` for a commodity is the near-month FUTCOM
        contract — so the REST call must receive that Future instrument.
        """
        from datetime import date

        from tradex_domain.instruments import Future

        broker, transport = _make_broker()
        rows = [
            {
                "symbol": "SILVERM-04Sep2026-FUT", "exchange": "MCX",
                "key": "MCX:510000", "security_id": "510000", "asset_class": "OTHER",
                "instrument_type": "FUTCOM", "right": None,
                "expiry": "2026-09-04", "underlying": "SILVERM",
            },
            {
                "symbol": "SILVERM-24Aug2026-279000-CE", "exchange": "MCX",
                "key": "MCX:1", "asset_class": "OTHER", "instrument_type": "OPTFUT",
                "right": "CE", "expiry": "2026-08-24", "strike": "279000.00000",
                "underlying": "SILVERM",
            },
            {
                "symbol": "SILVERM-24Aug2026-279000-PE", "exchange": "MCX",
                "key": "MCX:2", "asset_class": "OTHER", "instrument_type": "OPTFUT",
                "right": "PE", "expiry": "2026-08-24", "strike": "279000.00000",
                "underlying": "SILVERM",
            },
        ]
        broker.load_instruments(rows)
        transport.get_option_chain.return_value = OptionChain(
            underlying=Equity.of("MCX", "SILVERM")
        )
        chain = broker.get_option_chain(Equity.of("MCX", "SILVERM"))
        assert isinstance(chain, OptionChain)
        # REST was consulted, and the scrip passed is the near-month FUTCOM.
        transport.get_option_chain.assert_called_once()
        rest_underlying = transport.get_option_chain.call_args.args[0]
        assert isinstance(rest_underlying, Future)
        assert rest_underlying.instrument_id.expiry == date(2026, 9, 4)
        assert rest_underlying.instrument_id.exchange == "MCX"

    def test_get_option_chain_mcx_master_fallback_when_rest_fails(self):
        """A failed MCX REST chain falls back to the master when it has strikes."""
        from datetime import date

        broker, transport = _make_broker()
        rows = [
            {
                "symbol": "SILVERM-24Aug2026-279000-CE", "exchange": "MCX",
                "key": "MCX:1", "asset_class": "OTHER", "instrument_type": "OPTFUT",
                "right": "CE", "expiry": "2026-08-24", "strike": "279000.00000",
                "underlying": "SILVERM",
            },
            {
                "symbol": "SILVERM-24Aug2026-279000-PE", "exchange": "MCX",
                "key": "MCX:2", "asset_class": "OTHER", "instrument_type": "OPTFUT",
                "right": "PE", "expiry": "2026-08-24", "strike": "279000.00000",
                "underlying": "SILVERM",
            },
        ]
        broker.load_instruments(rows)
        transport.get_option_chain.side_effect = BrokerUnavailableError("provider down")
        chain = broker.get_option_chain(Equity.of("MCX", "SILVERM"))
        expiries = chain.expiries()
        assert len(expiries) == 1
        assert expiries[0].expiry_date == date(2026, 8, 24)
        assert len(expiries[0].pairs) == 1
        transport.get_option_chain.assert_called_once()

    def test_get_option_chain_master_fallback_when_rest_fails(self):
        """A failed NFO REST chain falls back to the master when it has strikes."""
        broker, transport = _make_broker()
        rows = [
            {
                "symbol": "NIFTY-24Oct2026-26000-CE", "exchange": "NFO", "key": "NFO:1",
                "asset_class": "OTHER", "instrument_type": "OPTIDX", "right": "CE",
                "expiry": "2026-10-24", "strike": "26000", "underlying": "NIFTY",
            },
            {
                "symbol": "NIFTY-24Oct2026-26000-PE", "exchange": "NFO", "key": "NFO:2",
                "asset_class": "OTHER", "instrument_type": "OPTIDX", "right": "PE",
                "expiry": "2026-10-24", "strike": "26000", "underlying": "NIFTY",
            },
        ]
        broker.load_instruments(rows)
        transport.get_option_chain.side_effect = BrokerUnavailableError("provider down")
        chain = broker.get_option_chain(Equity.of("NFO", "NIFTY"))
        assert len(chain.expiries()) == 1
        assert len(chain.expiries()[0].pairs) == 1
        transport.get_option_chain.assert_called_once()

    def test_get_option_chain_no_fallback_for_non_transport_errors(self):
        """Only transport-level failures fall back to the master chain; a
        validation/auth failure must propagate, never masquerade as data."""
        broker, transport = _make_broker()
        rows = [
            {
                "symbol": "NIFTY-24Oct2026-26000-CE", "exchange": "NFO", "key": "NFO:1",
                "asset_class": "OTHER", "instrument_type": "OPTIDX", "right": "CE",
                "expiry": "2026-10-24", "strike": "26000", "underlying": "NIFTY",
            },
            {
                "symbol": "NIFTY-24Oct2026-26000-PE", "exchange": "NFO", "key": "NFO:2",
                "asset_class": "OTHER", "instrument_type": "OPTIDX", "right": "PE",
                "expiry": "2026-10-24", "strike": "26000", "underlying": "NIFTY",
            },
        ]
        broker.load_instruments(rows)
        transport.get_option_chain.side_effect = ValueError("bad request shape")
        with pytest.raises(ValueError, match="bad request shape"):
            broker.get_option_chain(Equity.of("NFO", "NIFTY"))
        transport.get_option_chain.assert_called_once()

    def test_search(self):
        broker, _ = _make_broker()
        broker.load_instruments()  # load fallback universe
        results = broker.search("RELIANCE")
        assert len(results) >= 1
        assert results[0].symbol == "RELIANCE"

    def test_search_empty(self):
        broker, _ = _make_broker()
        broker.load_instruments()
        results = broker.search("NONEXISTENT")
        assert results == []


# ---------------------------------------------------------------------------
# Extension methods (super/forever/slice/eDIS)
# ---------------------------------------------------------------------------


class TestExtensionMethods:
    def test_submit_super_order(self):
        broker, transport = _make_broker()
        transport.submit_super_order.return_value = OrderId(value="SO1")
        request = OrderRequest(
            instrument=_equity(),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("100")),
            target_price=Price(value=Decimal("110")),
            stop_loss_price=Price(value=Decimal("95")),
        )
        result = broker.submit_super_order(request)
        assert result.value == "SO1"

    def test_submit_forever_order(self):
        broker, transport = _make_broker()
        transport.submit_forever_order.return_value = OrderId(value="FO1")
        request = OrderRequest(
            instrument=_equity(),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("100")),
            trigger_price=Price(value=Decimal("99")),
        )
        result = broker.submit_forever_order(request)
        assert result.value == "FO1"

    def test_submit_slice_order(self):
        broker, transport = _make_broker()
        transport.submit_slice_order.return_value = [OrderId(value="S1")]
        request = OrderRequest(
            instrument=_equity(),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(value=Decimal("10")),
        )
        result = broker.submit_slice_order(request, slices=1)
        assert len(result) == 1

    def test_submit_edis(self):
        broker, transport = _make_broker()
        transport.submit_edis.return_value = OrderId(value="E1")
        request = OrderRequest(
            instrument=_equity(),
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=Quantity(value=Decimal("10")),
        )
        result = broker.submit_edis(request)
        assert result.value == "E1"

    def test_list_super_orders(self):
        broker, transport = _make_broker()
        transport.list_super_orders.return_value = [{"id": "SO1"}]
        result = broker.list_super_orders()
        assert len(result) == 1

    def test_list_forever_orders(self):
        broker, transport = _make_broker()
        transport.list_forever_orders.return_value = [{"id": "FO1"}]
        result = broker.list_forever_orders()
        assert len(result) == 1

    def test_kill_switch(self):
        broker, transport = _make_broker()
        transport.kill_switch.return_value = {"status": "ACTIVE"}
        result = broker.kill_switch(enable=True)
        assert result["status"] == "ACTIVE"

    def test_status_kill_switch(self):
        broker, transport = _make_broker()
        transport.status_kill_switch.return_value = {"status": "ACTIVE"}
        result = broker.status_kill_switch()
        assert result["status"] == "ACTIVE"


# ---------------------------------------------------------------------------
# Auxiliary account methods
# ---------------------------------------------------------------------------


class TestAuxiliaryAccount:
    def test_profile(self):
        broker, transport = _make_broker()
        transport.profile.return_value = {"clientName": "Test"}
        result = broker.profile()
        assert result["clientName"] == "Test"

    def test_ledger(self):
        broker, transport = _make_broker()
        transport.ledger.return_value = [{"date": "2026-08-01"}]
        result = broker.ledger("2026-08-01", "2026-08-05")
        assert len(result) == 1

    def test_fund_limits(self):
        broker, transport = _make_broker()
        transport.fund_limits.return_value = {"balance": 50000}
        result = broker.fund_limits()
        assert result["balance"] == 50000

    def test_margin_calculator(self):
        broker, transport = _make_broker()
        transport.margin_calculator.return_value = {"margin": 5000}
        result = broker.margin_calculator(_equity(), side=OrderSide.BUY, quantity=10)
        assert result["margin"] == 5000

    def test_token_status(self):
        broker, transport = _make_broker()
        transport.token_status.return_value = {"valid": True}
        result = broker.token_status()
        assert result["valid"] is True

    def test_get_trade_history(self):
        broker, transport = _make_broker()
        transport.get_trade_history.return_value = [{"tradeId": "T1"}]
        result = broker.get_trade_history()
        assert len(result) == 1

    def test_trade_book(self):
        broker, transport = _make_broker()
        transport.trade_book.return_value = [{"tradeId": "T1"}]
        result = broker.trade_book()
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Instrument loading tests
# ---------------------------------------------------------------------------


class TestInstruments:
    def test_load_fallback_universe(self):
        broker, _ = _make_broker()
        broker.load_instruments()
        results = broker.search("NIFTY")
        assert len(results) >= 1

    def test_load_custom_rows(self):
        broker, _ = _make_broker()
        rows = [
            {"symbol": "TCS", "exchange": "NSE", "key": "2038", "asset_class": "EQUITY"},
        ]
        broker.load_instruments(rows)
        results = broker.search("TCS")
        assert len(results) >= 1

    def test_registry_property(self):
        broker, _ = _make_broker()
        assert broker.registry is not None

    def test_future_chain(self):
        broker, _ = _make_broker()
        broker.load_instruments()
        result = broker.future_chain(_equity())
        assert isinstance(result, list)

    def test_load_instruments_builds_typed_derivatives(self):
        """MCX rows become real Option/Future instruments, not Equity."""
        from datetime import date

        from tradex_domain.enums import AssetClass

        broker, _ = _make_broker()
        rows = [
            {
                "symbol": "SILVER-04Sep2026-FUT",
                "exchange": "MCX",
                "key": "MCX:510000",
                "security_id": "510000",
                "asset_class": "OTHER",
                "instrument_type": "FUTCOM",
                "right": None,
                "expiry": "2026-09-04",
                "underlying": "SILVER",
            },
            {
                "symbol": "SILVERM-24Aug2026-279000-CE",
                "exchange": "MCX",
                "key": "MCX:509665",
                "security_id": "509665",
                "asset_class": "OTHER",
                "instrument_type": "OPTFUT",
                "right": "CE",
                "expiry": "2026-08-24",
                "strike": "279000.00000",
                "underlying": "SILVERM",
            },
        ]
        broker.load_instruments(rows)
        futures = [i for i in broker._loaded_instruments if i.asset_class is AssetClass.FUTURE]
        options = [i for i in broker._loaded_instruments if i.asset_class is AssetClass.OPTION]
        assert len(futures) == 1
        assert len(options) == 1
        assert futures[0].instrument_id.expiry == date(2026, 9, 4)
        assert options[0].instrument_id.strike == Decimal("279000")
        assert options[0].instrument_id.right == "CE"
        # Provider keys and bare security-id aliases still resolve.
        assert broker.registry.provider_key(options[0].instrument_id) == "MCX:509665"
        assert broker.registry.resolve("509665") == options[0].instrument_id

    def test_load_derivatives_registers_contract_meta(self):
        """Master-derived meta carries instrument_type/lot_size/tick_size so
        ``_history_instrument_type`` and quant consumers see real contract data."""
        broker, _ = _make_broker()
        rows = [
            {
                "symbol": "SILVER-04Sep2026-FUT",
                "exchange": "MCX",
                "key": "MCX:510000",
                "security_id": "510000",
                "asset_class": "FUTURE",
                "instrument_type": "FUTCOM",
                "right": None,
                "expiry": "2026-09-04",
                "underlying": "SILVER",
                "lot_size": "30",
                "tick_size": "0.5",
            },
        ]
        broker.load_instruments(rows)
        fut = broker._loaded_instruments[0]
        meta = broker.registry.meta(fut.instrument_id)
        assert meta["asset_class"] == "FUTURE"
        assert meta["instrument_type"] == "FUTCOM"
        assert meta["lot_size"] == "30"
        assert meta["tick_size"] == "0.5"

    def test_future_chain_from_loaded_master(self):
        """future_chain() returns typed Future instruments from the master."""
        from datetime import date

        from tradex_domain.enums import AssetClass

        broker, _ = _make_broker()
        rows = [
            {
                "symbol": "GOLD-04Sep2026-FUT", "exchange": "MCX", "key": "MCX:1",
                "asset_class": "OTHER", "instrument_type": "FUTCOM",
                "expiry": "2026-09-04", "underlying": "GOLD",
            },
            {
                "symbol": "GOLD-30Oct2026-FUT", "exchange": "MCX", "key": "MCX:2",
                "asset_class": "OTHER", "instrument_type": "FUTCOM",
                "expiry": "2026-10-30", "underlying": "GOLD",
            },
            {
                "symbol": "SILVER-04Sep2026-FUT", "exchange": "MCX", "key": "MCX:3",
                "asset_class": "OTHER", "instrument_type": "FUTCOM",
                "expiry": "2026-09-04", "underlying": "SILVER",
            },
        ]
        broker.load_instruments(rows)
        chain = broker.future_chain(Equity.of("MCX", "GOLD"))
        assert [c.instrument_id.expiry for c in chain] == [date(2026, 9, 4), date(2026, 10, 30)]
        assert all(c.asset_class is AssetClass.FUTURE for c in chain)


# ---------------------------------------------------------------------------
# Streaming tests
# ---------------------------------------------------------------------------


class TestStreaming:
    def test_stream_backend(self):
        broker, transport = _make_broker()
        transport.order_stream_backend.return_value = "mock_stream"
        result = broker.stream_backend()
        assert result == "mock_stream"

    def test_market_stream_backend(self):
        broker, transport = _make_broker()
        transport.market_stream_backend.return_value = "mock_market"
        result = broker.market_stream_backend()
        assert result == "mock_market"

    def test_depth_stream_backend(self):
        broker, transport = _make_broker()
        transport.depth_stream_backend.return_value = "mock_depth"
        result = broker.depth_stream_backend()
        assert result == "mock_depth"

    def test_stream_backend_no_transport(self):
        broker = DhanBroker()
        with pytest.raises(BrokerUnavailableError):
            broker.stream_backend()

    def test_market_stream_backend_no_transport(self):
        broker = DhanBroker()
        with pytest.raises(BrokerUnavailableError):
            broker.market_stream_backend()

    def test_depth_stream_backend_no_transport(self):
        broker = DhanBroker()
        with pytest.raises(BrokerUnavailableError):
            broker.depth_stream_backend()


# ---------------------------------------------------------------------------
# Not-connected guard tests
# ---------------------------------------------------------------------------


class TestNotConnected:
    def test_orders_require_connection(self):
        transport = MagicMock(spec=DhanApiClient)
        broker = DhanBroker(transport=transport)
        # Not connected yet
        with pytest.raises(BrokerUnavailableError):
            broker.get_orderbook()

    def test_portfolio_requires_connection(self):
        transport = MagicMock(spec=DhanApiClient)
        broker = DhanBroker(transport=transport)
        with pytest.raises(BrokerUnavailableError):
            broker.get_positions()

    def test_market_data_requires_connection(self):
        transport = MagicMock(spec=DhanApiClient)
        broker = DhanBroker(transport=transport)
        with pytest.raises(BrokerUnavailableError):
            broker.ltp(_equity())


# ---------------------------------------------------------------------------
# Depth stream wiring
# ---------------------------------------------------------------------------


class TestDepthStreamWiring:
    """subscribe_depth routes to the dedicated depth-20 backend, not _ws_backend."""

    def test_subscribe_depth_uses_dedicated_backend(self) -> None:
        from tradex_domain.instruments import Equity

        from tradex_brokers.dhan.adapter import DhanBroker

        depth_backend = MagicMock()
        depth_backend.subscribe_depth.return_value = "sub-1"
        transport = MagicMock()
        transport.depth_stream_backend.return_value = depth_backend
        broker = DhanBroker(transport=transport)
        # The market backend has no subscribe_depth — the old path would raise.
        broker._ws_backend = MagicMock()
        result = broker.subscribe_depth([Equity.of("NSE", "RELIANCE")], lambda d: None)
        assert result == "sub-1"
        transport.depth_stream_backend.assert_called_once()
        depth_backend.subscribe_depth.assert_called_once()

    def test_subscribe_depth_reuses_backend(self) -> None:
        from tradex_domain.instruments import Equity

        from tradex_brokers.dhan.adapter import DhanBroker

        depth_backend = MagicMock()
        transport = MagicMock()
        transport.depth_stream_backend.return_value = depth_backend
        broker = DhanBroker(transport=transport)
        broker.subscribe_depth([Equity.of("NSE", "RELIANCE")], lambda d: None)
        broker.subscribe_depth([Equity.of("NSE", "RELIANCE")], lambda d: None)
        transport.depth_stream_backend.assert_called_once()

    def test_subscribe_depth_unbound_returns_none(self) -> None:
        from tradex_domain.instruments import Equity

        from tradex_brokers.dhan.adapter import DhanBroker

        broker = DhanBroker()
        assert broker.subscribe_depth([Equity.of("NSE", "RELIANCE")], lambda d: None) is None

    def test_close_closes_depth_backend(self) -> None:
        from tradex_domain.instruments import Equity

        from tradex_brokers.dhan.adapter import DhanBroker

        depth_backend = MagicMock()
        transport = MagicMock()
        transport.depth_stream_backend.return_value = depth_backend
        broker = DhanBroker(transport=transport)
        broker.subscribe_depth([Equity.of("NSE", "RELIANCE")], lambda d: None)
        broker.close()
        depth_backend.close.assert_called_once()
