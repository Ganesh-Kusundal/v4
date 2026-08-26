"""Tests for the v4 Upstox REST client (tradex_brokers.upstox.client)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest
from support.fake_fetch import FakeFetch
from tradex_domain.enums import (
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
    Timeframe,
)
from tradex_domain.errors import CapabilityNotSupportedError
from tradex_domain.execution import Order, OrderRequest, Position
from tradex_domain.instruments import Equity, Index, Instrument, Option
from tradex_domain.market import Depth, HistoricalSeries, Quote
from tradex_domain.value_objects import (
    AccountId,
    CorrelationId,
    InstrumentId,
    OrderId,
    Price,
    Quantity,
)
from tradex_domain.wire import InstrumentRegistry

from tradex_brokers.common.client_shared import correlation_id
from tradex_brokers.common.provider_common import instrument_from_id, parse_date
from tradex_brokers.common.resilience import MultiBucketRateLimiter, RateLimitConfig
from tradex_brokers.common.ws_reconnect import WSReconnectManager
from tradex_brokers.upstox.client import (
    UpstoxApiClient,
    _as_price,
    _first_mapping,
    _unwrap_data,
)

# ---------------------------------------------------------------------------
# Fixtures
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


def _make_client(
    responses: list[dict] | None = None,
) -> tuple[UpstoxApiClient, FakeFetch, InstrumentRegistry]:
    """Build a client over a fake fetch (no network, no MagicMock).

    A single response becomes the universal body; multiple responses are
    served in call order (matching the old ``MagicMock`` side_effect).
    """
    response_list = responses or [{}]
    fake = FakeFetch()
    if len(response_list) == 1:
        fake.default(response_list[0])
    else:
        fake.ordered(response_list)
    registry = _make_registry()
    client = UpstoxApiClient.from_fetch(
        fetch=fake, registry=registry, access_token="test-token"
    )
    return client, fake, registry


def _equity() -> Instrument:
    return Equity.of("NSE", "RELIANCE")


def _index() -> Instrument:
    return Index.of("IDX", "NIFTY")


# ---------------------------------------------------------------------------
# Helper tests
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_as_price_none(self):
        p = _as_price(None)
        assert p.value == Decimal("0")

    def test_as_price_float(self):
        p = _as_price(100.5)
        assert p.value == Decimal("100.5")

    def test_as_price_string(self):
        p = _as_price("250.75")
        assert p.value == Decimal("250.75")

    def test_parse_date_none(self):
        assert parse_date(None) is None

    def test_parse_date_iso(self):
        d = parse_date("2026-08-05")
        assert d is not None
        assert d.year == 2026
        assert d.month == 8

    def test_unwrap_data_with_envelope(self):
        assert _unwrap_data({"data": [1, 2]}) == [1, 2]

    def test_unwrap_data_without_envelope(self):
        assert _unwrap_data([1, 2]) == [1, 2]

    def test_first_mapping_from_dict(self):
        assert _first_mapping({"a": 1}) == {"a": 1}

    def test_first_mapping_from_list(self):
        assert _first_mapping([{"a": 1}]) == {"a": 1}

    def test_first_mapping_empty(self):
        assert _first_mapping([]) == {}

    def test_correlation_id_uuid(self):
        cid = correlation_id("12345678-1234-5678-1234-567812345678", fallback_seed="x")
        assert isinstance(cid, CorrelationId)

    def test_correlation_id_fallback(self):
        cid = correlation_id(None, fallback_seed="test-seed")
        assert isinstance(cid, CorrelationId)

    def test_instrument_from_id_equity(self):
        iid = InstrumentId.equity("NSE", "RELIANCE")
        inst = instrument_from_id(iid)
        assert isinstance(inst, Instrument)
        assert inst.symbol == "RELIANCE"

    def test_instrument_from_id_option(self):
        from datetime import date
        iid = InstrumentId.option("NFO", "NIFTY", date(2026, 8, 28), Decimal("24000"), "CE")
        inst = instrument_from_id(iid)
        assert isinstance(inst, Option)


# ---------------------------------------------------------------------------
# Client internal mapping tests
# ---------------------------------------------------------------------------


class TestClientInternals:
    def test_native_product_type_intraday(self):
        assert UpstoxApiClient._native_product_type(ProductType.INTRADAY) == "I"

    def test_native_product_type_delivery(self):
        assert UpstoxApiClient._native_product_type(ProductType.DELIVERY) == "D"

    def test_native_product_type_cover(self):
        assert UpstoxApiClient._native_product_type(ProductType.COVER_ORDER) == "CO"

    def test_native_product_type_margin_raises(self):
        with pytest.raises(ValueError, match="does not support"):
            UpstoxApiClient._native_product_type(ProductType.MARGIN)

    def test_domain_order_type_sl(self):
        assert UpstoxApiClient._domain_order_type("SL") == OrderType.STOP_LIMIT

    def test_domain_order_type_slm(self):
        assert UpstoxApiClient._domain_order_type("SL-M") == OrderType.STOP

    def test_domain_order_type_market(self):
        assert UpstoxApiClient._domain_order_type("MARKET") == OrderType.MARKET

    def test_has_order_identity_true_token(self):
        assert UpstoxApiClient._has_order_identity({"instrument_token": "xyz"}) is True

    def test_has_order_identity_true_tradingsymbol(self):
        assert UpstoxApiClient._has_order_identity({"tradingsymbol": "RELIANCE"}) is True

    def test_has_order_identity_false(self):
        assert UpstoxApiClient._has_order_identity({}) is False


# ---------------------------------------------------------------------------
# Order method tests
# ---------------------------------------------------------------------------


class TestOrderMethods:
    def test_submit_order(self):
        response = {"data": {"order_id": "12345"}}
        client, http, _ = _make_client([response])
        request = OrderRequest(
            instrument=_equity(),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(value=Decimal("10")),
            product_type=ProductType.INTRADAY,
        )
        order_id = client.submit_order(request)
        assert isinstance(order_id, OrderId)
        assert order_id.value == "12345"

    def test_submit_order_missing_id_raises(self):
        response = {"data": {}}
        client, _, _ = _make_client([response])
        request = OrderRequest(
            instrument=_equity(),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(value=Decimal("10")),
        )
        with pytest.raises(ValueError, match="missing order_id"):
            client.submit_order(request)

    def test_cancel_order(self):
        response = {
            "data": {
                "order_id": "99",
                "instrument_token": "NSE_EQ|INE002A01018",
                "status": "cancelled",
                "transaction_type": "BUY",
                "order_type": "MARKET",
                "quantity": 10,
                "filled_quantity": 0,
            }
        }
        client, http, _ = _make_client([response])
        order = client.cancel_order(OrderId(value="99"))
        assert isinstance(order, Order)
        assert order.status == OrderStatus.CANCELLED

    def test_modify_order(self):
        response = {
            "data": {
                "order_id": "99",
                "instrument_token": "NSE_EQ|INE002A01018",
                "status": "pending",
                "transaction_type": "BUY",
                "order_type": "LIMIT",
                "quantity": 10,
                "price": 100.0,
                "filled_quantity": 0,
            }
        }
        client, http, _ = _make_client([response])
        request = OrderRequest(
            instrument=_equity(),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("100")),
        )
        order = client.modify_order(OrderId(value="99"), request)
        assert isinstance(order, Order)

    def test_get_order(self):
        response = {
            "data": {
                "order_id": "42",
                "instrument_token": "NSE_EQ|INE002A01018",
                "status": "complete",
                "transaction_type": "BUY",
                "order_type": "MARKET",
                "quantity": 5,
                "filled_quantity": 5,
            }
        }
        client, _, _ = _make_client([response])
        order = client.get_order(OrderId(value="42"))
        assert order.status == OrderStatus.FILLED
        assert order.order_id.value == "42"

    def test_get_orderbook(self):
        response = {
            "data": [
                {
                    "order_id": "1",
                    "instrument_token": "NSE_EQ|INE002A01018",
                    "status": "open",
                    "transaction_type": "BUY",
                    "order_type": "MARKET",
                    "quantity": 10,
                    "filled_quantity": 0,
                }
            ]
        }
        client, _, _ = _make_client([response])
        orders = client.get_orderbook()
        assert len(orders) == 1
        assert orders[0].order_id.value == "1"

    def test_get_order_by_correlation_id(self):
        response = {
            "data": [
                {"order_id": "1", "tag": "abc", "instrument_token": "NSE_EQ|INE002A01018"},
                {"order_id": "2", "tag": "xyz", "instrument_token": "NSE_EQ|INE002A01018"},
            ]
        }
        client, _, _ = _make_client([response])
        result = client.get_order_by_correlation_id("abc")
        assert result["order_id"] == "1"

    def test_get_order_by_correlation_id_not_found(self):
        response = {"data": []}
        client, _, _ = _make_client([response])
        result = client.get_order_by_correlation_id("missing")
        assert result == {}


# ---------------------------------------------------------------------------
# Extension order tests
# ---------------------------------------------------------------------------


class TestExtensionOrders:
    def test_submit_super_order_raises(self):
        client, _, _ = _make_client()
        request = OrderRequest(
            instrument=_equity(),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("100")),
        )
        with pytest.raises(CapabilityNotSupportedError):
            client.submit_super_order(request)

    def test_submit_forever_order(self):
        response = {"data": {"order_id": "FO1"}}
        client, _, _ = _make_client([response])
        request = OrderRequest(
            instrument=_equity(),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("100")),
            trigger_price=Price(value=Decimal("99")),
        )
        oid = client.submit_forever_order(request)
        assert oid.value == "FO1"

    def test_submit_slice_order(self):
        # Each slice calls submit_order, so we need multiple responses
        responses = [
            {"data": {"order_id": "S1"}},
            {"data": {"order_id": "S2"}},
            {"data": {"order_id": "S3"}},
        ]
        client, _, _ = _make_client(responses)
        request = OrderRequest(
            instrument=_equity(),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(value=Decimal("30")),
        )
        ids = client.submit_slice_order(request, slices=3, interval=None)
        assert len(ids) == 3

    def test_submit_slice_order_with_interval_raises(self):
        client, _, _ = _make_client()
        request = OrderRequest(
            instrument=_equity(),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(value=Decimal("10")),
        )
        with pytest.raises(CapabilityNotSupportedError):
            client.submit_slice_order(request, slices=2, interval=timedelta(seconds=60))

    def test_submit_edis_raises(self):
        client, _, _ = _make_client()
        request = OrderRequest(
            instrument=_equity(),
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=Quantity(value=Decimal("10")),
        )
        with pytest.raises(CapabilityNotSupportedError):
            client.submit_edis(request)

    def test_list_forever_orders(self):
        client, _, _ = _make_client()
        result = client.list_forever_orders()
        assert result == []

    def test_modify_forever_order(self):
        response = {"data": {"status": "modified"}}
        client, _, _ = _make_client([response])
        request = OrderRequest(
            instrument=_equity(),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("10")),
            trigger_price=Price(value=Decimal("99")),
        )
        result = client.modify_forever_order(OrderId(value="FO1"), request)
        assert result.status == OrderStatus.UNKNOWN  # "modified" is not a valid OrderStatus

    def test_cancel_forever_order(self):
        response = {"data": {"status": "cancelled"}}
        client, _, _ = _make_client([response])
        result = client.cancel_forever_order(OrderId(value="FO1"))
        assert result.status == OrderStatus.CANCELLED


# ---------------------------------------------------------------------------
# Account / profile / funds tests
# ---------------------------------------------------------------------------


class TestAccountMethods:
    def test_get_account(self):
        response = {
            "data": {
                "available_to_trade": {"total": 50000.0},
            }
        }
        client, _, _ = _make_client([response])
        snapshot = client.get_account()
        assert snapshot.account_id == AccountId(value="upstox")
        assert snapshot.balance is not None
        assert snapshot.balance.amount == Decimal("50000.0")

    def test_get_account_legacy_format(self):
        response = {
            "data": {
                "equity": {"available_margin": 25000.0},
            }
        }
        client, _, _ = _make_client([response])
        snapshot = client.get_account()
        assert snapshot.balance.amount == Decimal("25000.0")

    def test_profile(self):
        response = {"data": {"clientName": "Test"}}
        client, _, _ = _make_client([response])
        result = client.profile()
        assert result["clientName"] == "Test"

    def test_token_status(self):
        response = {"data": {"isValid": True}}
        client, _, _ = _make_client([response])
        result = client.token_status()
        assert result["isValid"] is True

    def test_fund_limits(self):
        response = {"data": {"available_to_trade": {"total": 100000}}}
        client, _, _ = _make_client([response])
        result = client.fund_limits()
        assert "available_to_trade" in result

    def test_ledger(self):
        response = {"data": [{"date": "2026-08-01", "amount": 500}]}
        client, _, _ = _make_client([response])
        result = client.ledger("2026-08-01", "2026-08-05")
        assert len(result) == 1

    def test_margin(self):
        response = {"data": {"margin": 5000}}
        client, _, _ = _make_client([response])
        result = client.margin(_equity(), side=OrderSide.BUY, quantity=10)
        assert result["margin"] == 5000

    def test_convert_position(self):
        response = {"data": {"status": "success"}}
        client, _, _ = _make_client([response])
        result = client.convert_position(
            _equity(),
            from_product=ProductType.INTRADAY,
            to_product=ProductType.DELIVERY,
            quantity=10,
        )
        assert result["status"] == "success"


# ---------------------------------------------------------------------------
# Market data tests
# ---------------------------------------------------------------------------


class TestMarketData:
    def test_ltp(self):
        key = "NSE_EQ|INE002A01018"
        response = {"data": {key: {"last_price": 2500.50, "instrument_token": key}}}
        client, _, _ = _make_client([response])
        price = client.ltp(_equity())
        assert price.value == Decimal("2500.5")

    def test_get_quote(self):
        key = "NSE_EQ|INE002A01018"
        response = {
            "data": {
                key: {
                    "last_price": 2500.0,
                    "volume": 100000,
                    "instrument_token": key,
                    "ohlc": {
                        "open": 2480.0,
                        "high": 2520.0,
                        "low": 2470.0,
                        "close": 2490.0,
                    },
                    "depth": {
                        "buy": [{"price": 2499.0, "quantity": 100}],
                        "sell": [{"price": 2501.0, "quantity": 50}],
                    },
                }
            }
        }
        client, _, _ = _make_client([response])
        quote = client.get_quote(_equity())
        assert isinstance(quote, Quote)
        assert quote.ltp.value == Decimal("2500")
        assert quote.volume.value == 100000

    def test_depth(self):
        key = "NSE_EQ|INE002A01018"
        response = {
            "data": {
                key: {
                    "instrument_token": key,
                    "depth": {
                        "buy": [{"price": 2499.0, "quantity": 100}],
                        "sell": [{"price": 2501.0, "quantity": 50}],
                    },
                }
            }
        }
        client, _, _ = _make_client([response])
        d = client.depth(_equity())
        assert isinstance(d, Depth)
        assert len(d.bids) == 1
        assert len(d.asks) == 1

    def test_ltp_batch(self):
        key = "NSE_EQ|INE002A01018"
        response = {"data": {key: {"last_price": 2500.0, "instrument_token": key}}}
        client, _, _ = _make_client([response])
        result = client.ltp_batch([_equity()])
        iid = InstrumentId.equity("NSE", "RELIANCE")
        assert iid in result
        assert result[iid].value == Decimal("2500")

    def test_ltp_batch_empty(self):
        client, _, _ = _make_client()
        result = client.ltp_batch([])
        assert result == {}

    def test_quote_batch(self):
        key = "NSE_EQ|INE002A01018"
        response = {
            "data": {
                key: {
                    "last_price": 2500.0,
                    "volume": 0,
                    "instrument_token": key,
                    "ohlc": {"open": 0, "high": 0, "low": 0, "close": 0},
                }
            }
        }
        client, _, _ = _make_client([response])
        result = client.quote_batch([_equity()])
        iid = InstrumentId.equity("NSE", "RELIANCE")
        assert iid in result

    def test_history_daily(self):
        response = {
            "data": {
                "candles": [
                    ["2026-08-01", 100.0, 105.0, 99.0, 104.0, 1000],
                    ["2026-08-02", 104.0, 106.0, 100.0, 105.0, 1200],
                ]
            }
        }
        client, _, _ = _make_client([response])
        start = datetime(2026, 8, 1, tzinfo=UTC)
        end = datetime(2026, 8, 5, tzinfo=UTC)
        series = client.history(_equity(), Timeframe.D1, start, end)
        assert isinstance(series, HistoricalSeries)
        assert len(series.candles) == 2

    def test_history_unsupported_timeframe(self):
        client, _, _ = _make_client()
        with pytest.raises(ValueError, match="unsupported"):
            client.history(
                _equity(), "2h",
                datetime(2026, 8, 1, tzinfo=UTC),
                datetime(2026, 8, 5, tzinfo=UTC),
            )


# ---------------------------------------------------------------------------
# Position / portfolio tests
# ---------------------------------------------------------------------------


class TestPositions:
    def test_get_positions(self):
        response = {
            "data": [
                {
                    "instrument_token": "NSE_EQ|INE002A01018",
                    "quantity": 10,
                    "average_price": 2500.0,
                    "realised": 100.0,
                    "unrealised": 50.0,
                }
            ]
        }
        client, _, _ = _make_client([response])
        positions = client.get_positions()
        assert len(positions) == 1
        assert isinstance(positions[0], Position)
        assert positions[0].quantity.value == Decimal("10")

    def test_get_holdings(self):
        response = {"data": []}
        client, _, _ = _make_client([response])
        assert client.get_holdings() == []

    def test_get_portfolio(self):
        responses = [
            # get_positions response (called first)
            {"data": []},
            # get_account response (called second via _account_for_portfolio)
            {"data": {"available_to_trade": {"total": 50000.0}}},
        ]
        client, _, _ = _make_client(responses)
        portfolio = client.get_portfolio()
        assert portfolio.account is not None
        assert portfolio.positions == []


# ---------------------------------------------------------------------------
# Financial data / news / extras tests
# ---------------------------------------------------------------------------


class TestNewsExtras:
    def test_get_news(self):
        # Real v2 shape: data maps each instrument key to an array of items.
        response = {"data": {"NSE_EQ|INE002A01018": [{"heading": "Market update"}]}}
        client, _, _ = _make_client([response])
        result = client.get_news("instrument_keys", instrument_keys=["NSE_EQ|INE002A01018"])
        assert len(result) == 1
        assert result[0]["heading"] == "Market update"

    def test_get_news_accepts_list_envelope(self):
        response = {"data": [{"heading": "Market update"}]}
        client, _, _ = _make_client([response])
        result = client.get_news("positions")
        assert len(result) == 1

    def test_get_news_requires_category(self):
        client, _, _ = _make_client()
        with pytest.raises(TypeError):
            client.get_news()

    def test_get_news_rejects_unknown_category(self):
        client, _, _ = _make_client()
        with pytest.raises(ValueError, match="invalid Upstox news category"):
            client.get_news("gossip")

    def test_get_news_requires_keys_for_instrument_keys_category(self):
        client, _, _ = _make_client()
        with pytest.raises(ValueError, match="instrument_keys is required"):
            client.get_news("instrument_keys")

    def test_get_news_rejects_over_30_keys(self):
        client, _, _ = _make_client()
        with pytest.raises(ValueError, match="at most 30"):
            client.get_news("instrument_keys", instrument_keys=[f"K{i}" for i in range(31)])

    def test_get_news_sends_real_v2_params(self):
        client, fake, _ = _make_client([{"data": {}}])
        client.get_news("instrument_keys", instrument_keys=["A", "B"], page_size=10)
        last = fake.last_call()
        assert last["url"].endswith("/news")
        params = last["kwargs"].get("params", {})
        assert params["category"] == "instrument_keys"
        assert params["instrument_keys"] == "A,B"
        assert params["page_size"] == 10

    def test_get_trade_book(self):
        response = {"data": [{"trade_id": "T1"}]}
        client, _, _ = _make_client([response])
        result = client.get_trade_book()
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Option chain tests
# ---------------------------------------------------------------------------


class TestOptionChain:
    """Upstox /option/chain mapping (placeholder-leg guard, Dhan parity)."""

    def test_chain_skips_placeholder_legs(self):
        """Rows whose call/put keys are falsy are dropped, not registered.

        Regression: a placeholder leg with an empty/zero instrument key
        collapsed to ``"0"``/``"None"`` and collided across strikes in the
        registry when the next placeholder row registered the same key. A
        paired leg with no mapped key would also break quote/order lookups.
        """
        responses = [
            {
                "data": {
                    "expiry": "2026-12-29",
                    "chain": [
                        {
                            "expiry": "2026-12-29",
                            "strike_price": 13000,
                            "call_options": {"instrument_key": ""},
                            "put_options": {"instrument_key": "0"},
                        },
                        {
                            "expiry": "2026-12-29",
                            "strike_price": 14000,
                            "call_options": {"instrument_key": "NFO_OPT|55001"},
                            "put_options": {"instrument_key": "NFO_OPT|55002"},
                        },
                    ],
                }
            }
        ]
        client, _, registry = _make_client(responses)
        chain = client.get_option_chain(_index(), expiry="2026-12-29")
        expiries = chain.expiries()
        assert len(expiries) == 1
        pairs = list(expiries[0].pairs)
        # Only the real contract (truthy keys on both legs) is paired.
        assert [p.strike.value for p in pairs] == [Decimal("14000")]
        # The real legs are registered under their native instrument keys.
        assert registry.resolve("NFO_OPT|55001") is not None
        assert registry.resolve("NFO_OPT|55002") is not None
        # No placeholder "0"/"None" key was registered.
        assert registry.resolve("0") is None
        assert registry.resolve("None") is None


# ---------------------------------------------------------------------------
# Kill switch / static IP tests
# ---------------------------------------------------------------------------


class TestKillSwitchStaticIP:
    def test_kill_switch(self):
        response = {"data": {"status": "ENABLED"}}
        client, _, _ = _make_client([response])
        result = client.kill_switch(enable=True)
        assert result["status"] == "ENABLED"

    def test_status_kill_switch(self):
        response = {"data": {"status": "ENABLED"}}
        client, _, _ = _make_client([response])
        result = client.status_kill_switch()
        assert result["status"] == "ENABLED"

    def test_exit_all(self):
        response = {"data": {"status": "DISABLED"}}
        client, _, _ = _make_client([response])
        result = client.exit_all()
        assert result["status"] == "DISABLED"


# ---------------------------------------------------------------------------
# Streaming tests
# ---------------------------------------------------------------------------


class TestStreaming:
    def test_portfolio_stream_backend(self):
        from tradex_brokers.upstox.ws_streams import UpstoxPortfolioStreamBackend

        client, _, _ = _make_client()
        backend = client.portfolio_stream_backend()
        assert isinstance(backend, UpstoxPortfolioStreamBackend)

    def test_market_stream_backend(self):
        from tradex_brokers.upstox.ws_streams import UpstoxMarketDataStreamBackend

        client, _, _ = _make_client()
        backend = client.market_stream_backend()
        assert isinstance(backend, UpstoxMarketDataStreamBackend)

    def test_order_stream_backend(self):
        client, _, _ = _make_client()
        backend = client.order_stream_backend()
        assert backend is not None

    def test_portfolio_stream_backend_materializes_with_ws_transport(self):
        """A WS-wired client returns a real UpstoxPortfolioStreamBackend."""
        from tradex_brokers.upstox.ws_streams import UpstoxPortfolioStreamBackend

        client = UpstoxApiClient.from_fetch(
            fetch=lambda *a, **k: (200, {}),
            registry=_make_registry(),
            access_token="tok",
            ws_fetch=lambda *a, **k: (200, {"data": {"authorized_redirect_uri": "wss://x"}}),
            ws_token_provider=lambda: "tok",
        )
        backend = client.portfolio_stream_backend()
        assert isinstance(backend, UpstoxPortfolioStreamBackend)
        assert backend._authorize_url == "https://api.upstox.com/v2/feed/portfolio-stream-feed/authorize"
        # The backend's mappers reuse the client's row mappers.
        assert callable(backend._map_order)
        assert callable(backend._map_position)
        backend.close()

    def test_invalidate_read_cache(self):
        client, http, _ = _make_client()
        client.invalidate_read_cache()


# ---------------------------------------------------------------------------
# Market-data backend subscribe modes
# ---------------------------------------------------------------------------


class TestMarketBackendSubscribeModes:
    """Quote (full) and depth-30 (full_d30) subs are tracked per mode."""

    def _backend(self):
        from tradex_brokers.upstox.ws_streams import UpstoxMarketDataStreamBackend

        registry = InstrumentRegistry()
        iid = InstrumentId.equity("NSE", "RELIANCE")
        registry.register(iid, {"key": "NSE_EQ|INE002A01018", "asset_class": "EQUITY"})
        registry.add_alias("NSE_EQ|INE002A01018", iid)
        ws = MagicMock()
        backend = UpstoxMarketDataStreamBackend(
            authorize_url="https://example/authorize",
            ws_fetch=lambda *a, **k: (200, {"data": {"authorized_redirect_uri": "wss://x"}}),
            token_provider=lambda: "tok",
            registry=registry,
            ws_factory=lambda url: ws,
        )
        backend._ws = ws  # bypass socket handshake; drive _send_subscribes
        return backend, ws

    def _sent_messages(self, ws):
        return [
            json.loads(call.args[0])
            for call in ws.send.call_args_list
        ]

    def test_quote_and_depth30_both_sent_for_same_key(self):
        backend, ws = self._backend()
        inst = _equity()
        backend.subscribe_quotes([inst], lambda q: None)
        backend.subscribe_depth_30([inst], lambda d: None)
        assert ws.send.call_count == 2
        modes = [m["data"]["mode"] for m in self._sent_messages(ws)]
        assert modes == ["full", "full_d30"]

    def test_same_mode_same_key_sent_once(self):
        backend, ws = self._backend()
        inst = _equity()
        backend.subscribe_quotes([inst], lambda q: None)
        backend.subscribe_quotes([inst], lambda q: None)
        assert ws.send.call_count == 1

    def test_quote_then_depth30_keeps_quote_dedupe_intact(self):
        backend, ws = self._backend()
        inst = _equity()
        backend.subscribe_quotes([inst], lambda q: None)
        backend.subscribe_depth_30([inst], lambda d: None)
        backend.subscribe_quotes([inst], lambda q: None)  # deduped against full
        assert ws.send.call_count == 2
        modes = [m["data"]["mode"] for m in self._sent_messages(ws)]
        assert modes == ["full", "full_d30"]


# ---------------------------------------------------------------------------
# Market-backend reconnect tests (AutoReconnectMixin)
# ---------------------------------------------------------------------------


class TestMarketBackendReconnect:
    """A dropped market socket is reopened and per-mode subs are replayed."""

    def _backend(self):
        import threading

        from tradex_brokers.upstox.ws_streams import UpstoxMarketDataStreamBackend

        registry = _make_registry()
        opened: list[Any] = []
        drop = threading.Event()

        class FakeWS:
            def __init__(self) -> None:
                self.sent: list[bytes] = []

            def send(self, data: bytes) -> None:
                self.sent.append(data)

            def recv(self) -> bytes:
                # Block until the test drops the socket, so the initial
                # subscriptions land deterministically before the failure.
                drop.wait(timeout=5)
                raise ConnectionError("socket dropped")

            def close(self) -> None:
                pass

        def factory(url: str) -> FakeWS:
            ws = FakeWS()
            opened.append(ws)
            return ws

        backend = UpstoxMarketDataStreamBackend(
            authorize_url="https://example/authorize",
            ws_fetch=lambda *a, **k: (200, {"data": {"authorized_redirect_uri": "wss://x"}}),
            token_provider=lambda: "tok",
            registry=registry,
            ws_factory=factory,
        )
        backend._reconnect = WSReconnectManager(
            max_retries=200, base_delay=0.01, max_delay=0.02, jitter=False
        )
        return backend, opened, drop

    def test_socket_drop_reopens_and_resubscribes_both_modes(self):
        import time

        backend, opened, drop = self._backend()
        inst = _equity()
        backend.subscribe_quotes([inst], lambda q: None)
        backend.subscribe_depth_30([inst], lambda d: None)
        # Both modes multiplexed onto the first socket.
        first_frames = [
            json.loads(raw.decode("utf-8"))
            for raw in opened[0].sent
            if isinstance(raw, (bytes, bytearray))
        ]
        assert {f["data"]["mode"] for f in first_frames} == {"full", "full_d30"}
        # Drop the socket; reconnect reopens and replays both modes.
        drop.set()
        deadline = time.monotonic() + 5
        while len(opened) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(opened) >= 2  # reconnect opened a fresh socket
        frames = [
            json.loads(raw.decode("utf-8"))
            for raw in opened[1].sent
            if isinstance(raw, (bytes, bytearray))
        ]
        modes = {f["data"]["mode"] for f in frames if f.get("method") == "sub"}
        assert modes == {"full", "full_d30"}
        keys = [
            k for f in frames for k in f.get("data", {}).get("instrumentKeys", [])
        ]
        assert "NSE_EQ|INE002A01018" in keys
        backend.close()

    def test_subscribe_during_reconnect_window_is_replayed(self):
        """A subscription made while the socket is down is not lost on reconnect."""
        import threading
        import time

        from tradex_brokers.upstox.ws_streams import UpstoxMarketDataStreamBackend

        opened: list[Any] = []
        drop = threading.Event()

        class FakeWS:
            def __init__(self) -> None:
                self.sent: list[bytes] = []

            def send(self, data: bytes) -> None:
                self.sent.append(data)

            def recv(self) -> bytes:
                drop.wait(timeout=5)
                raise ConnectionError("socket dropped")

            def close(self) -> None:
                pass

        def factory(url: str) -> FakeWS:
            ws = FakeWS()
            opened.append(ws)
            return ws

        backend = UpstoxMarketDataStreamBackend(
            authorize_url="https://example/authorize",
            ws_fetch=lambda *a, **k: (200, {"data": {"authorized_redirect_uri": "wss://x"}}),
            token_provider=lambda: "tok",
            registry=_make_registry(),
            ws_factory=factory,
        )
        # Long backoff creates a deterministic reconnect window.
        backend._reconnect = WSReconnectManager(
            max_retries=200, base_delay=1.0, max_delay=2.0, jitter=False
        )
        inst = _equity()
        backend.subscribe_quotes([inst], lambda q: None)
        drop.set()  # kill socket 0; reconnect worker is now sleeping ~1s
        time.sleep(0.05)
        # Subscribe depth-30 while the socket is down — the key must be
        # recorded and replayed on the fresh socket, not lost.
        backend.subscribe_depth_30([inst], lambda d: None)
        deadline = time.monotonic() + 6
        while len(opened) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(opened) >= 2
        frames = [
            json.loads(raw.decode("utf-8"))
            for raw in opened[1].sent
            if isinstance(raw, (bytes, bytearray))
        ]
        modes = {f["data"]["mode"] for f in frames if f.get("method") == "sub"}
        assert modes == {"full", "full_d30"}  # depth-30 sub survived the window
        backend.close()

    def test_reconnect_disabled_keeps_single_socket(self):
        import time

        from tradex_brokers.upstox.ws_streams import UpstoxMarketDataStreamBackend

        opened: list[Any] = []

        class FakeWS:
            def recv(self) -> bytes:
                raise ConnectionError("socket dropped")

            def close(self) -> None:
                pass

        def factory(url: str) -> FakeWS:
            ws = FakeWS()
            opened.append(ws)
            return ws

        backend = UpstoxMarketDataStreamBackend(
            authorize_url="https://example/authorize",
            ws_fetch=lambda *a, **k: (200, {"data": {"authorized_redirect_uri": "wss://x"}}),
            token_provider=lambda: "tok",
            registry=_make_registry(),
            ws_factory=factory,
            reconnect=False,
        )
        backend.subscribe_quotes([_equity()], lambda q: None)
        time.sleep(0.1)  # a reconnect attempt must not run
        assert len(opened) == 1
        backend.close()


# ---------------------------------------------------------------------------
# Market-backend unsubscribe lifecycle (wire-set pruning + live unsub frames)
# ---------------------------------------------------------------------------


class TestMarketBackendUnsubscribe:
    """Unsubscribe prunes the per-mode wire set and sends live unsub frames."""

    def _backend(self):
        """Backend with a pre-wired socket — drives subscribe/unsub frames."""
        from tradex_brokers.upstox.ws_streams import UpstoxMarketDataStreamBackend

        class FakeWS:
            def __init__(self) -> None:
                self.sent: list[bytes] = []

            def send(self, data: bytes) -> None:
                self.sent.append(data)

            def recv(self) -> bytes:
                raise ConnectionError("socket dropped")

            def close(self) -> None:
                pass

        ws = FakeWS()
        backend = UpstoxMarketDataStreamBackend(
            authorize_url="https://example/authorize",
            ws_fetch=lambda *a, **k: (200, {"data": {"authorized_redirect_uri": "wss://x"}}),
            token_provider=lambda: "tok",
            registry=_make_registry(),
            ws_factory=lambda url: FakeWS(),
            reconnect=False,
        )
        backend._ws = ws  # bypass socket handshake; drive subscribe/unsub frames
        return backend, ws

    @staticmethod
    def _frames(ws) -> list[dict]:
        return [
            json.loads(raw.decode("utf-8"))
            for raw in ws.sent
            if isinstance(raw, (bytes, bytearray))
        ]

    def test_unsubscribe_depth_sends_unsub_frame_and_prunes_only_that_mode(self) -> None:
        backend, ws = self._backend()
        inst = _equity()
        quote_sub = backend.subscribe_quotes([inst], lambda q: None)
        depth_sub = backend.subscribe_depth_30([inst], lambda d: None)
        assert "NSE_EQ|INE002A01018" in backend._subscribed_keys["full"]
        assert "NSE_EQ|INE002A01018" in backend._subscribed_keys["full_d30"]
        # Dropping the depth-30 sub must unsub the key from that mode only.
        backend.unsubscribe(depth_sub)
        unsub_frames = [f for f in self._frames(ws) if f.get("method") == "unsub"]
        assert len(unsub_frames) == 1
        assert unsub_frames[0]["data"]["mode"] == "full_d30"
        assert unsub_frames[0]["data"]["instrumentKeys"] == ["NSE_EQ|INE002A01018"]
        # The quote-mode key survives; the depth-30 mode is fully pruned.
        assert "NSE_EQ|INE002A01018" in backend._subscribed_keys["full"]
        assert "NSE_EQ|INE002A01018" not in backend._subscribed_keys.get("full_d30", [])
        backend.unsubscribe(quote_sub)
        assert "NSE_EQ|INE002A01018" not in backend._subscribed_keys.get("full", [])
        assert backend._subscribed_keys == {}
        assert backend._subscribed_instruments == []
        backend.close()

    def test_unsubscribe_instruments_prunes_all_modes(self) -> None:
        backend, ws = self._backend()
        inst = _equity()
        backend.subscribe_quotes([inst], lambda q: None)
        backend.subscribe_depth_30([inst], lambda d: None)
        backend.unsubscribe_instruments([inst])
        frames = self._frames(ws)
        unsub_modes = {f["data"]["mode"] for f in frames if f.get("method") == "unsub"}
        assert unsub_modes == {"full", "full_d30"}
        assert backend._subscribed_keys == {}
        assert backend._subscribed_instruments == []
        assert backend._instrument_refs == {}
        backend.close()

    def test_ensure_ws_replays_full_live_set_on_fresh_open(self) -> None:
        """A fresh manual open heals the pre-outage set (backoff-exhaustion path)."""
        import threading
        import time

        from tradex_brokers.upstox.ws_streams import UpstoxMarketDataStreamBackend

        opened: list[Any] = []
        drop = threading.Event()

        class FakeWS:
            def __init__(self) -> None:
                self.sent: list[bytes] = []

            def send(self, data: bytes) -> None:
                self.sent.append(data)

            def recv(self) -> bytes:
                drop.wait(timeout=5)  # hold socket 0 open until the test drops it
                raise ConnectionError("socket dropped")

            def close(self) -> None:
                pass

        def factory(url: str) -> FakeWS:
            ws = FakeWS()
            opened.append(ws)
            return ws

        backend = UpstoxMarketDataStreamBackend(
            authorize_url="https://example/authorize",
            ws_fetch=lambda *a, **k: (200, {"data": {"authorized_redirect_uri": "wss://x"}}),
            token_provider=lambda: "tok",
            registry=_make_registry(),
            ws_factory=factory,
            reconnect=False,
        )
        inst = _equity()
        backend.subscribe_quotes([inst], lambda q: None)
        backend.subscribe_depth_30([inst], lambda d: None)
        assert len(opened) == 1  # both modes multiplexed onto one socket
        drop.set()  # socket 0's receive loop fails and clears _ws (no reconnect)
        deadline = time.monotonic() + 2
        while backend._ws is not None and time.monotonic() < deadline:
            time.sleep(0.01)
        # Simulate the post-exhaustion state: socket gone, backoff spent.
        backend._reconnect._attempt = backend._reconnect._max_retries
        backend._ensure_ws()
        assert len(opened) == 2
        frames = self._frames(opened[1])
        modes = {f["data"]["mode"] for f in frames if f.get("method") == "sub"}
        assert modes == {"full", "full_d30"}  # the full pre-outage set replayed
        assert backend._reconnect.attempt_count == 0  # backoff reset on heal
        backend.close()


# ---------------------------------------------------------------------------
# Portfolio-backend reconnect tests (AutoReconnectMixin)
# ---------------------------------------------------------------------------


class TestPortfolioBackendReconnect:
    """A dropped Upstox portfolio socket re-authorizes and reopens."""

    def _backend(self, opened: list[Any], *, reconnect: bool = True):
        from tradex_brokers.upstox.ws_streams import UpstoxPortfolioStreamBackend

        class FakeWS:
            def __init__(self) -> None:
                self.sent: list[bytes] = []

            def send(self, data: bytes) -> None:
                self.sent.append(data)

            def recv(self) -> bytes:
                raise ConnectionError("socket dropped")

            def close(self) -> None:
                pass

        def factory(url: str) -> FakeWS:
            ws = FakeWS()
            opened.append(ws)
            return ws

        backend = UpstoxPortfolioStreamBackend(
            authorize_url="https://example/authorize",
            ws_fetch=lambda *a, **k: (
                200,
                {"data": {"authorized_redirect_uri": "wss://x"}},
            ),
            token_provider=lambda: "tok",
            map_order=lambda row: MagicMock(),
            ws_factory=factory,
            reconnect=reconnect,
        )
        return backend

    def test_socket_drop_reauthorizes_and_reopens(self) -> None:
        import time

        opened: list[Any] = []
        backend = self._backend(opened)
        backend._reconnect = WSReconnectManager(
            max_retries=200, base_delay=0.01, max_delay=0.02, jitter=False
        )
        backend.subscribe_orders(MagicMock())
        assert len(opened) == 1
        deadline = time.monotonic() + 5
        while len(opened) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(opened) >= 2  # reconnect authorized + opened a fresh socket
        backend.close()

    def test_reconnect_disabled_keeps_single_socket(self) -> None:
        import time

        opened: list[Any] = []
        backend = self._backend(opened, reconnect=False)
        backend.subscribe_orders(MagicMock())
        time.sleep(0.1)  # a reconnect attempt must not run
        assert len(opened) == 1
        backend.close()


# ---------------------------------------------------------------------------
# Expired option data test
# ---------------------------------------------------------------------------


class TestExtendedEndpoints:
    def test_get_ohlc(self):
        response = {
            "data": {
                "NSE_EQ:RELIANCE": {
                    "ohlc": {"open": 100, "high": 110, "low": 90, "close": 105},
                    "instrument_token": "NSE_EQ|INE002A01018",
                }
            }
        }
        client, _, _ = _make_client([response])
        result = client.get_ohlc([_equity()])
        ohlc = result[_equity().instrument_id]
        assert ohlc.open.value == 100
        assert ohlc.high.value == 110
        assert ohlc.low.value == 90
        assert ohlc.close.value == 105

    def test_get_ohlc_too_many_keys(self):
        client, _, _ = _make_client()
        instruments = [_equity() for _ in range(501)]
        with pytest.raises(ValueError):
            client.get_ohlc(instruments)

    def test_intraday_candles(self):
        response = {
            "data": {
                "candles": [["2025-01-12T15:15:00+05:30", 100, 101, 99, 100.5, 1000, 0]]
            }
        }
        client, _, _ = _make_client([response])
        series = client.intraday_candles(_equity(), Timeframe.M5)
        assert len(series.candles) == 1
        assert series.candles[0].ohlc.close.value == 100.5

    def test_intraday_candles_rejects_weekly(self):
        client, _, _ = _make_client()
        with pytest.raises(ValueError):
            client.intraday_candles(_equity(), Timeframe.W1)


def test_upstox_api_client_exposes_rate_limiter():
    limiter = MultiBucketRateLimiter(default=RateLimitConfig())
    http = MagicMock()
    http.rate_limiter = limiter
    client = UpstoxApiClient(http=http, registry=MagicMock())

    assert client.rate_limiter is limiter
