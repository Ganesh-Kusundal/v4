"""Tests for the v4 Upstox broker adapter (tradex_brokers.upstox.adapter)."""

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
from tradex_domain.market import OHLC, Depth, HistoricalSeries, Quote
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

from tradex_brokers.upstox.adapter import UpstoxBroker
from tradex_brokers.upstox.client import UpstoxApiClient

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_registry() -> InstrumentRegistry:
    reg = InstrumentRegistry()
    iid = InstrumentId.equity("NSE", "RELIANCE")
    reg.register(iid, {"key": "NSE_EQ|INE002A01018", "asset_class": "EQUITY"})
    reg.add_alias("NSE_EQ|INE002A01018", iid)
    reg.add_alias("RELIANCE", iid)
    idx_id = InstrumentId.equity("IDX", "NIFTY")
    reg.register(idx_id, {"key": "NSE_INDEX|Nifty 50", "asset_class": "INDEX"})
    reg.add_alias("NSE_INDEX|Nifty 50", idx_id)
    reg.add_alias("NIFTY", idx_id)
    return reg


def _equity() -> Instrument:
    return Equity.of("NSE", "RELIANCE")


def _index() -> Instrument:
    return Index.of("IDX", "NIFTY")


def _make_broker() -> tuple[UpstoxBroker, MagicMock]:
    """Build a connected adapter with a mocked UpstoxApiClient transport."""
    transport = MagicMock(spec=UpstoxApiClient)
    registry = _make_registry()
    broker = UpstoxBroker(transport=transport, registry=registry)
    broker.connect()
    return broker, transport


# ---------------------------------------------------------------------------
# Lifecycle tests
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_connect_without_transport_is_noop(self):
        broker = UpstoxBroker()
        broker.connect()  # Should not raise — no-op without transport

    def test_connect_with_transport(self):
        transport = MagicMock(spec=UpstoxApiClient)
        broker = UpstoxBroker(transport=transport)
        broker.connect()

    def test_close(self):
        broker, _ = _make_broker()
        broker.close()
        with pytest.raises(BrokerUnavailableError):
            broker.get_orderbook()

    def test_verify_connection_success(self):
        broker, transport = _make_broker()
        transport.get_account.return_value = Account(
            account_id=AccountId(value="upstox"),
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
        assert caps.supports_forever_order is True
        assert caps.supports_slice_order is True
        assert caps.supports_kill_switch is True
        assert caps.supports_news is True
        assert caps.supports_fundamentals is True


# ---------------------------------------------------------------------------
# Mutation gate tests
# ---------------------------------------------------------------------------


class TestMutationGate:
    def test_submit_order_when_disabled(self):
        transport = MagicMock(spec=UpstoxApiClient)
        broker = UpstoxBroker(transport=transport, allow_order_operations=False)
        broker.connect()
        with pytest.raises(OrderRejectedError):
            broker.submit_order(MagicMock())

    def test_set_order_operations_enabled(self):
        broker, _ = _make_broker()
        broker.set_order_operations_enabled(False)
        with pytest.raises(OrderRejectedError):
            broker.submit_order(MagicMock())
        broker.set_order_operations_enabled(True)
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

    def test_get_order_by_correlation_id(self):
        broker, transport = _make_broker()
        transport.get_order_by_correlation_id.return_value = {"order_id": "1"}
        result = broker.get_order_by_correlation_id("abc")
        assert result["order_id"] == "1"


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
            account_id=AccountId(value="upstox"),
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
        result = broker.get_option_chain(eq, expiry="2026-08-28")
        assert isinstance(result, OptionChain)

    def test_get_option_chain_no_expiry_raises(self):
        broker, _ = _make_broker()
        with pytest.raises(CapabilityNotSupportedError):
            broker.get_option_chain(_equity())

    def test_get_option_chain_mcx_from_master(self):
        """MCX has no Upstox REST chain endpoint — the master serves the chain."""
        from datetime import date

        from tradex_domain.instruments import Option as OptionInstrument

        broker, transport = _make_broker()
        rows = [
            {
                "symbol": "SILVER 226000 CE 26 MAY 27", "exchange": "MCX",
                "key": "MCX_FO|579520", "asset_class": "OTHER",
                "instrument_type": "CE", "right": "CE", "expiry": "2027-05-26",
                "strike": 226000.0, "underlying": "SILVER",
            },
            {
                "symbol": "SILVER 226000 PE 26 MAY 27", "exchange": "MCX",
                "key": "MCX_FO|579521", "asset_class": "OTHER",
                "instrument_type": "PE", "right": "PE", "expiry": "2027-05-26",
                "strike": 226000.0, "underlying": "SILVER",
            },
        ]
        broker.load_instruments(rows)
        chain = broker.get_option_chain(Equity.of("MCX", "SILVER"))
        expiries = chain.expiries()
        assert len(expiries) == 1
        assert expiries[0].expiry_date == date(2027, 5, 26)
        assert len(expiries[0].pairs) == 1
        pair = expiries[0].pairs[0]
        assert isinstance(pair.call, OptionInstrument) and pair.call.right == "CE"
        assert isinstance(pair.put, OptionInstrument) and pair.put.right == "PE"
        transport.get_option_chain.assert_not_called()

    def test_get_option_chain_master_respects_expiry_filter(self):
        """An expiry argument narrows the master-derived chain."""
        broker, transport = _make_broker()
        rows = []
        for strike in (220000, 226000):
            rows.append({
                "symbol": f"SILVER {strike} CE 26 MAY 27", "exchange": "MCX",
                "key": f"MCX_FO|c{strike}", "asset_class": "OTHER",
                "instrument_type": "CE", "right": "CE", "expiry": "2027-05-26",
                "strike": strike, "underlying": "SILVER",
            })
            rows.append({
                "symbol": f"SILVER {strike} PE 26 MAY 27", "exchange": "MCX",
                "key": f"MCX_FO|p{strike}", "asset_class": "OTHER",
                "instrument_type": "PE", "right": "PE", "expiry": "2027-05-26",
                "strike": strike, "underlying": "SILVER",
            })
        broker.load_instruments(rows)
        chain = broker.get_option_chain(Equity.of("MCX", "SILVER"), expiry="2027-05-26")
        assert len(chain.expiries()) == 1
        assert len(chain.expiries()[0].pairs) == 2
        # No master rows for a different expiry → falls back to the REST path.
        broker.get_option_chain(Equity.of("MCX", "SILVER"), expiry="2026-08-24")
        transport.get_option_chain.assert_called_once()

    def test_search(self):
        broker, _ = _make_broker()
        broker.load_instruments()
        results = broker.search("RELIANCE")
        assert len(results) >= 1
        assert results[0].symbol == "RELIANCE"

    def test_search_empty(self):
        broker, _ = _make_broker()
        broker.load_instruments()
        results = broker.search("NONEXISTENT")
        assert results == []


# ---------------------------------------------------------------------------
# Extension methods (forever/slice/cover/eDIS)
# ---------------------------------------------------------------------------


class TestExtensionMethods:
    def test_submit_super_order_raises(self):
        broker, _ = _make_broker()
        with pytest.raises(CapabilityNotSupportedError):
            broker.submit_super_order(MagicMock())

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

    def test_submit_edis_raises(self):
        broker, _ = _make_broker()
        with pytest.raises(CapabilityNotSupportedError):
            broker.submit_edis(MagicMock())

    def test_modify_forever_order(self):
        broker, transport = _make_broker()
        transport.modify_forever_order.return_value = {"status": "modified"}
        request = OrderRequest(
            instrument=_equity(),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("10")),
            trigger_price=Price(value=Decimal("99")),
        )
        result = broker.modify_forever_order(OrderId(value="FO1"), request)
        assert result["status"] == "modified"

    def test_cancel_forever_order(self):
        broker, transport = _make_broker()
        transport.cancel_forever_order.return_value = {"status": "cancelled"}
        result = broker.cancel_forever_order(OrderId(value="FO1"))
        assert result["status"] == "cancelled"

    def test_list_forever_orders(self):
        broker, transport = _make_broker()
        transport.list_forever_orders.return_value = []
        result = broker.list_forever_orders()
        assert result == []

    def test_place_cover_order(self):
        broker, transport = _make_broker()
        transport.place_cover_order.return_value = OrderId(value="CO1")
        request = OrderRequest(
            instrument=_equity(),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(value=Decimal("10")),
            product_type=ProductType.COVER_ORDER,
        )
        result = broker.place_cover_order(request, stop_loss=Price(value=Decimal("95")))
        assert result.value == "CO1"

    def test_exit_cover_order(self):
        broker, transport = _make_broker()
        transport.exit_cover_order.return_value = {"status": "exited"}
        result = broker.exit_cover_order(OrderId(value="CO1"))
        assert result["status"] == "exited"


# ---------------------------------------------------------------------------
# Kill switch / auxiliary account
# ---------------------------------------------------------------------------


class TestKillSwitchAuxiliary:
    def test_kill_switch(self):
        broker, transport = _make_broker()
        transport.kill_switch.return_value = {"status": "ENABLED"}
        result = broker.kill_switch(enable=True)
        assert result["status"] == "ENABLED"

    def test_status_kill_switch(self):
        broker, transport = _make_broker()
        transport.status_kill_switch.return_value = {"status": "ENABLED"}
        result = broker.status_kill_switch()
        assert result["status"] == "ENABLED"

    def test_profile(self):
        broker, transport = _make_broker()
        transport.profile.return_value = {"clientName": "Test"}
        result = broker.profile()
        assert result["clientName"] == "Test"

    def test_token_status(self):
        broker, transport = _make_broker()
        transport.token_status.return_value = {"valid": True}
        result = broker.token_status()
        assert result["valid"] is True

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

    def test_margin(self):
        broker, transport = _make_broker()
        transport.margin.return_value = {"margin": 5000}
        result = broker.margin(_equity(), side=OrderSide.BUY, quantity=10)
        assert result["margin"] == 5000

    def test_get_trade_book(self):
        broker, transport = _make_broker()
        transport.get_trade_book.return_value = [{"trade_id": "T1"}]
        result = broker.get_trade_book()
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Upstox-specific extensions
# ---------------------------------------------------------------------------


class TestUpstoxSpecificExtensions:
    def test_expiry_list(self):
        broker, transport = _make_broker()
        transport.expiry_list.return_value = ["2026-08-28", "2026-09-25"]
        result = broker.expiry_list("NSE_INDEX|Nifty 50")
        assert len(result) == 2

    def test_get_cash_flow(self):
        broker, transport = _make_broker()
        transport.get_cash_flow.return_value = {"operating": 5000}
        result = broker.get_cash_flow("INE002A01018")
        assert result["operating"] == 5000

    def test_get_ratios(self):
        broker, transport = _make_broker()
        transport.get_ratios.return_value = {"pe": 25.5}
        result = broker.get_ratios("INE002A01018")
        assert result["pe"] == 25.5

    def test_get_financials(self):
        broker, transport = _make_broker()
        transport.get_financials.return_value = {"revenue": 100000}
        result = broker.get_financials("INE002A01018", "profit-loss")
        assert result["revenue"] == 100000

    def test_get_balance_sheet(self):
        broker, transport = _make_broker()
        transport.get_balance_sheet.return_value = {"assets": 50000}
        result = broker.get_balance_sheet("INE002A01018")
        assert result["assets"] == 50000

    def test_get_pnl(self):
        broker, transport = _make_broker()
        transport.get_pnl.return_value = {"net_profit": 10000}
        result = broker.get_pnl("INE002A01018")
        assert result["net_profit"] == 10000

    def test_get_news(self):
        broker, transport = _make_broker()
        transport.get_news.return_value = [{"heading": "Market update"}]
        result = broker.get_news("positions")
        assert len(result) == 1
        transport.get_news.assert_called_once_with(
            "positions", instrument_keys=None, page_number=None, page_size=None
        )

    def test_get_static_ip(self):
        broker, transport = _make_broker()
        transport.get_static_ip.return_value = {"primary_ip": "1.2.3.4"}
        result = broker.get_static_ip()
        assert result["primary_ip"] == "1.2.3.4"

    def test_set_static_ip(self):
        broker, transport = _make_broker()
        transport.set_static_ip.return_value = {"status": "updated"}
        result = broker.set_static_ip(primary="1.2.3.4")
        assert result["status"] == "updated"


class TestExtendedEndpoints:
    """New Upstox V2/V3 endpoints, thin pass-throughs to the client."""

    def test_get_ohlc(self):
        broker, transport = _make_broker()
        iid = InstrumentId.equity("NSE", "RELIANCE")
        transport.get_ohlc.return_value = {
            iid: OHLC(
                open=Price(Decimal(100)),
                high=Price(Decimal(110)),
                low=Price(Decimal(90)),
                close=Price(Decimal(105)),
            )
        }
        result = broker.get_ohlc([_equity()])
        assert result[iid].close.value == 105

    def test_intraday_candles(self):
        broker, transport = _make_broker()
        series = MagicMock()
        transport.intraday_candles.return_value = series
        result = broker.intraday_candles(_equity(), Timeframe.M5)
        assert result is series

    def test_get_option_contracts(self):
        broker, transport = _make_broker()
        transport.get_option_contracts.return_value = [{"strike_price": 19650}]
        result = broker.get_option_contracts(_index())
        assert result[0]["strike_price"] == 19650

    def test_get_change_oi(self):
        broker, transport = _make_broker()
        transport.get_change_oi.return_value = {"total_call_change_oi": 100}
        result = broker.get_change_oi(_index(), "2026-05-29", "2026-05-07", 2)
        assert result["total_call_change_oi"] == 100

    def test_get_max_pain(self):
        broker, transport = _make_broker()
        transport.get_max_pain.return_value = {"max_pain": 19600}
        result = broker.get_max_pain(_index(), "2026-05-29", "2026-05-07")
        assert result["max_pain"] == 19600

    def test_get_market_holidays(self):
        broker, transport = _make_broker()
        transport.get_market_holidays.return_value = [{"holiday": "2026-08-15"}]
        result = broker.get_market_holidays()
        assert result[0]["holiday"] == "2026-08-15"

    def test_get_market_timings(self):
        broker, transport = _make_broker()
        transport.get_market_timings.return_value = [{"exchange": "NSE"}]
        result = broker.get_market_timings("2026-08-06")
        assert result[0]["exchange"] == "NSE"

    def test_get_exchange_status(self):
        broker, transport = _make_broker()
        transport.get_exchange_status.return_value = {"status": "open"}
        result = broker.get_exchange_status("NSE")
        assert result["status"] == "open"

    def test_get_company_profile(self):
        broker, transport = _make_broker()
        transport.get_company_profile.return_value = {"name": "Reliance"}
        result = broker.get_company_profile("INE002A01018")
        assert result["name"] == "Reliance"

    def test_get_income_statement(self):
        broker, transport = _make_broker()
        transport.get_income_statement.return_value = {"type": "consolidated"}
        result = broker.get_income_statement("INE002A01018")
        assert result["type"] == "consolidated"

    def test_get_share_holdings(self):
        broker, transport = _make_broker()
        transport.get_share_holdings.return_value = [{"category": "promoters"}]
        result = broker.get_share_holdings("INE002A01018")
        assert result[0]["category"] == "promoters"

    def test_get_corporate_actions(self):
        broker, transport = _make_broker()
        transport.get_corporate_actions.return_value = [{"name": "Dividend"}]
        result = broker.get_corporate_actions("INE002A01018")
        assert result[0]["name"] == "Dividend"

    def test_get_competitors(self):
        broker, transport = _make_broker()
        transport.get_competitors.return_value = [{"sector": "Refineries"}]
        result = broker.get_competitors("INE002A01018")
        assert result[0]["sector"] == "Refineries"

    def test_get_trade_pnl(self):
        broker, transport = _make_broker()
        transport.get_trade_pnl.return_value = {"metadata": {"page": {"page_number": 1}}}
        result = broker.get_trade_pnl("EQ", "2324")
        assert result["metadata"]["page"]["page_number"] == 1

    def test_get_expired_option_data(self):
        broker, transport = _make_broker()
        transport.get_expired_option_data.return_value = {"candles": []}
        result = broker.get_expired_option_data(_equity())
        assert result == {"candles": []}


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
            {"symbol": "TCS", "exchange": "NSE", "key": "NSE:TCS", "asset_class": "EQUITY"},
        ]
        broker.load_instruments(rows)
        results = broker.search("TCS")
        assert len(results) >= 1

    def test_registry_property(self):
        broker, _ = _make_broker()
        assert broker.registry is not None

    def test_load_instruments_builds_typed_derivatives(self):
        """Upstox MCX rows become real Option/Future instruments."""
        from datetime import date

        from tradex_domain.enums import AssetClass

        broker, _ = _make_broker()
        rows = [
            {
                "symbol": "GOLD 120000 CE 30 OCT 26",
                "exchange": "MCX",
                "key": "MCX_FO|579316",
                "asset_class": "OTHER",
                "instrument_type": "CE",
                "right": "CE",
                "expiry": "2026-10-30",
                "strike": 120000.0,
                "underlying": "GOLD",
            },
            {
                "symbol": "GOLD 04SEP26 FUT",
                "exchange": "MCX",
                "key": "MCX_FO|559933",
                "asset_class": "OTHER",
                "instrument_type": "FUT",
                "expiry": "2026-09-04",
                "underlying": "GOLD",
            },
        ]
        broker.load_instruments(rows)
        options = [i for i in broker._loaded_instruments if i.asset_class is AssetClass.OPTION]
        futures = [i for i in broker._loaded_instruments if i.asset_class is AssetClass.FUTURE]
        assert len(options) == 1
        assert len(futures) == 1
        assert options[0].instrument_id.expiry == date(2026, 10, 30)
        assert options[0].instrument_id.right == "CE"
        assert futures[0].instrument_id.expiry == date(2026, 9, 4)
        assert broker.registry.provider_key(options[0].instrument_id) == "MCX_FO|579316"

    def test_load_derivatives_registers_contract_meta(self):
        """Upstox master-derived meta carries instrument_type/lot_size."""
        broker, _ = _make_broker()
        rows = [
            {
                "symbol": "GOLD 120000 CE 30 OCT 26",
                "exchange": "MCX",
                "key": "MCX_FO|579316",
                "asset_class": "OPTION",
                "instrument_type": "CE",
                "right": "CE",
                "expiry": "2026-10-30",
                "strike": "120000",
                "underlying": "GOLD",
                "lot_size": "1",
            },
        ]
        broker.load_instruments(rows)
        opt = broker._loaded_instruments[0]
        meta = broker.registry.meta(opt.instrument_id)
        assert meta["asset_class"] == "OPTION"
        assert meta["instrument_type"] == "CE"
        assert meta["lot_size"] == "1"

    def test_future_chain_from_loaded_master(self):
        """future_chain() returns typed Future instruments from the master."""
        from datetime import date

        from tradex_domain.enums import AssetClass

        broker, _ = _make_broker()
        rows = [
            {
                "symbol": "GOLD 04SEP26 FUT", "exchange": "MCX", "key": "MCX_FO|1",
                "asset_class": "OTHER", "instrument_type": "FUT",
                "expiry": "2026-09-04", "underlying": "GOLD",
            },
            {
                "symbol": "GOLD 30OCT26 FUT", "exchange": "MCX", "key": "MCX_FO|2",
                "asset_class": "OTHER", "instrument_type": "FUT",
                "expiry": "2026-10-30", "underlying": "GOLD",
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
        transport.portfolio_stream_backend.return_value = "mock_stream"
        result = broker.stream_backend()
        assert result == "mock_stream"

    def test_market_stream_backend(self):
        broker, transport = _make_broker()
        transport.market_stream_backend.return_value = "mock_market"
        result = broker.market_stream_backend()
        assert result == "mock_market"

    def test_stream_backend_no_transport(self):
        broker = UpstoxBroker()
        with pytest.raises(BrokerUnavailableError):
            broker.stream_backend()

    def test_market_stream_backend_no_transport(self):
        broker = UpstoxBroker()
        with pytest.raises(BrokerUnavailableError):
            broker.market_stream_backend()


# ---------------------------------------------------------------------------
# Not-connected guard tests
# ---------------------------------------------------------------------------


class TestNotConnected:
    def test_orders_require_connection(self):
        transport = MagicMock(spec=UpstoxApiClient)
        broker = UpstoxBroker(transport=transport)
        with pytest.raises(BrokerUnavailableError):
            broker.get_orderbook()

    def test_portfolio_requires_connection(self):
        transport = MagicMock(spec=UpstoxApiClient)
        broker = UpstoxBroker(transport=transport)
        with pytest.raises(BrokerUnavailableError):
            broker.get_positions()

    def test_market_data_requires_connection(self):
        transport = MagicMock(spec=UpstoxApiClient)
        broker = UpstoxBroker(transport=transport)
        with pytest.raises(BrokerUnavailableError):
            broker.ltp(_equity())


# ---------------------------------------------------------------------------
# Depth stream wiring
# ---------------------------------------------------------------------------


class TestDepthStreamWiring:
    """subscribe_depth_30 delegates to the market-data stream backend."""

    def test_subscribe_depth_30_delegates(self) -> None:
        from tradex_domain.instruments import Equity

        from tradex_brokers.upstox.adapter import UpstoxBroker

        ws = MagicMock()
        ws.subscribe_depth_30.return_value = "sub-30"
        broker = UpstoxBroker()
        broker._ws_backend = ws
        result = broker.subscribe_depth_30([Equity.of("NSE", "RELIANCE")], lambda d: None)
        assert result == "sub-30"
        ws.subscribe_depth_30.assert_called_once()

    def test_subscribe_depth_30_without_backend(self) -> None:
        from tradex_domain.instruments import Equity

        from tradex_brokers.upstox.adapter import UpstoxBroker

        broker = UpstoxBroker()
        assert broker.subscribe_depth_30([Equity.of("NSE", "RELIANCE")], lambda d: None) is None
