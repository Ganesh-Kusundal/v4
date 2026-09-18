"""Tests for the v4 Dhan REST client (tradex_brokers.dhan.client)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from support.fake_fetch import FakeFetch
from tradex_domain.enums import (
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
    Timeframe,
)
from tradex_domain.errors import CapabilityNotSupportedError, RateLimitError, SDKError
from tradex_domain.execution import Order, OrderRequest, Position
from tradex_domain.instruments import Equity, Future, Index, Instrument, Option
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
from tradex_brokers.dhan.client import (
    DhanApiClient,
    _as_price,
    _first_mapping,
    _unwrap_data,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_registry() -> InstrumentRegistry:
    reg = InstrumentRegistry()
    # Register a test equity
    iid = InstrumentId.equity("NSE", "RELIANCE")
    reg.register(iid, {"key": "2885", "asset_class": "EQUITY"})
    reg.add_alias("2885", iid)
    reg.add_alias("RELIANCE", iid)
    # Register a test index
    idx_id = InstrumentId.equity("IDX", "NIFTY")
    reg.register(idx_id, {"key": "13", "asset_class": "INDEX"})
    reg.add_alias("13", idx_id)
    reg.add_alias("NIFTY", idx_id)
    return reg


def _make_client(
    responses: list[dict] | None = None,
) -> tuple[DhanApiClient, FakeFetch, InstrumentRegistry]:
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
    client = DhanApiClient.from_fetch(
        fetch=fake, registry=registry, client_id="TEST123"
    )
    return client, fake, registry


def _equity() -> Instrument:
    return Equity.of("NSE", "RELIANCE")


def _index() -> Instrument:
    return Index.of("IDX", "NIFTY")


# ---------------------------------------------------------------------------
# Helper tests
# ---------------------------------------------------------------------------


class TestHistoryInstrumentType:
    """_history_instrument_type resolves the native Dhan history instrument
    type from master metadata (regression: meta used to carry OTHER for every
    derivative, so futures/options fell through to EQUITY)."""

    def _client_with(self, iid, meta) -> tuple[DhanApiClient, Instrument]:
        client, _, registry = _make_client()
        registry.register_authoritative(iid, "MCX:1", meta)
        return client, instrument_from_id(iid)

    def test_future_uses_native_kind(self):
        from datetime import date

        from tradex_domain.instruments import Future

        fut = Future.of("MCX", "SILVER", date(2026, 9, 4))
        client, inst = self._client_with(
            fut.instrument_id,
            {"asset_class": "FUTURE", "instrument_type": "FUTCOM"},
        )
        assert client._history_instrument_type(inst) == "FUTCOM"

    def test_option_uses_native_kind(self):
        from datetime import date

        from tradex_domain.instruments import Option

        opt = Option.of("NFO", "NIFTY", date(2026, 8, 27), 22600, "CE")
        client, inst = self._client_with(
            opt.instrument_id,
            {"asset_class": "OPTION", "instrument_type": "OPTIDX"},
        )
        assert client._history_instrument_type(inst) == "OPTIDX"

    def test_equity_falls_back_to_equity(self):
        client, inst = self._client_with(
            InstrumentId.equity("NSE", "RELIANCE"),
            {"asset_class": "EQUITY", "instrument_type": "EQ"},
        )
        assert client._history_instrument_type(inst) == "EQUITY"

    def test_native_kind_not_trusted_for_degraded_equity(self):
        """A CA/PA row that degraded to Equity must not be sent as OPTIDX."""
        client, inst = self._client_with(
            InstrumentId.equity("IDX", "NIFTY"),
            {"asset_class": "OTHER", "instrument_type": "OPTIDX"},
        )
        # Falls through to the exchange/asset-class heuristics (EQUITY here).
        assert client._history_instrument_type(inst) == "EQUITY"


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
    def test_segment_equity(self):
        assert DhanApiClient._segment(_equity()) == "NSE_EQ"

    def test_segment_index(self):
        assert DhanApiClient._segment(_index()) == "IDX_I"

    def test_native_product_type(self):
        assert DhanApiClient._native_product_type(ProductType.INTRADAY) == "INTRADAY"
        assert DhanApiClient._native_product_type(ProductType.DELIVERY) == "CNC"
        assert DhanApiClient._native_product_type(ProductType.COVER_ORDER) == "CO"

    def test_domain_order_type_stop_loss_with_price(self):
        assert DhanApiClient._domain_order_type("STOP_LOSS", has_price=True) == OrderType.STOP_LIMIT

    def test_domain_order_type_stop_loss_without_price(self):
        assert DhanApiClient._domain_order_type("STOP_LOSS", has_price=False) == OrderType.STOP

    def test_domain_order_type_stop_loss_market(self):
        assert DhanApiClient._domain_order_type("STOP_LOSS_MARKET") == OrderType.STOP

    def test_has_order_identity_true(self):
        assert DhanApiClient._has_order_identity({"securityId": "2885"}) is True

    def test_has_order_identity_false(self):
        assert DhanApiClient._has_order_identity({}) is False

    def test_option_exchange_nse(self):
        assert DhanApiClient._option_exchange(_equity()) == "NFO"

    def test_option_exchange_bse(self):
        bse_eq = Equity.of("BSE", "RELIANCE")
        assert DhanApiClient._option_exchange(bse_eq) == "BFO"

    def test_option_exchange_mcx(self):
        """Commodity underlyings keep their own exchange (not NFO)."""
        from datetime import date

        mcx_fut = Future.of("MCX", "CRUDEOIL", date(2026, 8, 19))
        assert DhanApiClient._option_exchange(mcx_fut) == "MCX"

    def test_option_exchange_nse_comm(self):
        from datetime import date

        fut = Future.of("NSE_COMM", "CRUDEOIL", date(2026, 8, 19))
        assert DhanApiClient._option_exchange(fut) == "NSE_COMM"


# ---------------------------------------------------------------------------
# Order method tests
# ---------------------------------------------------------------------------


class TestOrderMethods:
    def test_submit_order(self):
        response = {"data": {"orderId": "12345"}}
        client, http, _ = _make_client([response])
        instrument = _equity()
        request = OrderRequest(
            instrument=instrument,
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
        with pytest.raises(ValueError, match="missing orderId"):
            client.submit_order(request)

    def test_cancel_order(self):
        response = {
            "data": {
                "orderId": "99",
                "securityId": "2885",
                "orderStatus": "CANCELLED",
                "transactionType": "BUY",
                "orderType": "MARKET",
                "quantity": 10,
                "filledQty": 0,
            }
        }
        client, http, _ = _make_client([response])
        order = client.cancel_order(OrderId(value="99"))
        assert isinstance(order, Order)
        assert order.status == OrderStatus.CANCELLED

    def test_modify_order(self):
        response = {
            "data": {
                "orderId": "99",
                "securityId": "2885",
                "orderStatus": "PENDING",
                "transactionType": "BUY",
                "orderType": "LIMIT",
                "quantity": 10,
                "price": 100.0,
                "filledQty": 0,
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
                "orderId": "42",
                "securityId": "2885",
                "orderStatus": "TRADED",
                "transactionType": "BUY",
                "orderType": "MARKET",
                "quantity": 5,
                "filledQty": 5,
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
                    "orderId": "1",
                    "securityId": "2885",
                    "orderStatus": "PENDING",
                    "transactionType": "BUY",
                    "orderType": "MARKET",
                    "quantity": 10,
                    "filledQty": 0,
                }
            ]
        }
        client, _, _ = _make_client([response])
        orders = client.get_orderbook()
        assert len(orders) == 1
        assert orders[0].order_id.value == "1"

    def test_get_order_list(self):
        response = {
            "data": [
                {
                    "orderId": "1",
                    "securityId": "2885",
                    "orderStatus": "PENDING",
                    "transactionType": "BUY",
                    "orderType": "MARKET",
                    "quantity": 10,
                    "filledQty": 0,
                }
            ]
        }
        client, _, _ = _make_client([response])
        orders = client.get_order_list(status="ALL")
        assert len(orders) == 1


# ---------------------------------------------------------------------------
# Super / forever / slice order tests
# ---------------------------------------------------------------------------


class TestExtensionOrders:
    def test_submit_super_order(self):
        response = {"data": {"orderId": "SO1"}}
        client, _, _ = _make_client([response])
        request = OrderRequest(
            instrument=_equity(),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("100")),
            target_price=Price(value=Decimal("110")),
            stop_loss_price=Price(value=Decimal("95")),
        )
        oid = client.submit_super_order(request)
        assert oid.value == "SO1"

    def test_submit_super_order_invalid_prices(self):
        client, _, _ = _make_client()
        request = OrderRequest(
            instrument=_equity(),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("100")),
            target_price=Price(value=Decimal("90")),  # below price for BUY
            stop_loss_price=Price(value=Decimal("95")),
        )
        with pytest.raises(ValueError, match="invalid for the order side"):
            client.submit_super_order(request)

    def test_submit_forever_order(self):
        response = {"data": {"orderId": "FO1"}}
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
        response = {"data": {"orderIds": ["S1", "S2", "S3"]}}
        client, _, _ = _make_client([response])
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

    def test_list_super_orders(self):
        response = {"data": [{"superOrderId": "SO1"}]}
        client, _, _ = _make_client([response])
        result = client.list_super_orders()
        assert len(result) == 1

    def test_list_forever_orders(self):
        response = {"data": [{"foreverOrderId": "FO1"}]}
        client, _, _ = _make_client([response])
        result = client.list_forever_orders()
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Account / profile / funds tests
# ---------------------------------------------------------------------------


class TestAccountMethods:
    def test_get_account(self):
        response = {
            "data": {
                "availableBalance": 50000.0,
                "utilizedMargin": 10000.0,
            }
        }
        client, _, _ = _make_client([response])
        snapshot = client.get_account()
        assert snapshot.account_id == AccountId(value="dhan")
        assert snapshot.balance is not None
        assert snapshot.balance.amount == Decimal("50000.0")

    def test_profile(self):
        response = {"data": {"clientName": "Test"}}
        client, _, _ = _make_client([response])
        result = client.profile()
        assert result["clientName"] == "Test"

    def test_fund_limits(self):
        # "availabelBalance" is Dhan's actual API spelling (not a typo)
        response = {"data": {"availabelBalance": 100000}}
        client, _, _ = _make_client([response])
        result = client.fund_limits()
        assert "availabelBalance" in result

    def test_ledger(self):
        response = {"data": [{"date": "2026-08-01", "amount": 500}]}
        client, _, _ = _make_client([response])
        result = client.ledger("2026-08-01", "2026-08-05")
        assert len(result) == 1

    def test_token_status(self):
        response = {"data": {"isValid": True}}
        client, _, _ = _make_client([response])
        result = client.token_status()
        assert result["isValid"] is True

    def test_margin_calculator(self):
        response = {"data": {"margin": 5000}}
        client, _, _ = _make_client([response])
        result = client.margin_calculator(
            _equity(), side=OrderSide.BUY, quantity=10
        )
        assert result["margin"] == 5000


# ---------------------------------------------------------------------------
# Market data tests
# ---------------------------------------------------------------------------


class TestMarketData:
    def test_ltp(self):
        response = {"data": {"NSE_EQ": {"2885": {"ltp": 2500.50}}}}
        client, _, _ = _make_client([response])
        price = client.ltp(_equity())
        assert price.value == Decimal("2500.5")

    def test_ltp_rate_limit_body_raises(self):
        """A 429 failure body must raise, not silently become Price(0)."""
        response = {"data": {"805": "Too many requests."}, "_http_status": 429}
        client, _, _ = _make_client([response])
        with pytest.raises(RateLimitError):
            client.ltp(_equity())

    def test_ltp_failed_status_body_raises(self):
        """A status: failed body must raise SDKError, not parse to zero."""
        response = {"data": {"805": "boom"}, "status": "failed"}
        client, _, _ = _make_client([response])
        with pytest.raises(SDKError):
            client.ltp(_equity())

    def test_get_quote_rate_limit_body_raises(self):
        """Failure bodies are rejected on the quote path too."""
        response = {"data": {"805": "Too many requests."}, "_http_status": 429}
        client, _, _ = _make_client([response])
        with pytest.raises(RateLimitError):
            client.get_quote(_equity())

    def test_ltp_batch_rate_limit_body_raises(self):
        """Batch LTP wiring rejects failure bodies too."""
        response = {"data": {"805": "Too many requests."}, "_http_status": 429}
        client, _, _ = _make_client([response])
        with pytest.raises(RateLimitError):
            client.ltp_batch([_equity()])

    def test_quote_batch_rate_limit_body_raises(self):
        """Batch quote wiring rejects failure bodies too."""
        response = {"data": {"805": "Too many requests."}, "_http_status": 429}
        client, _, _ = _make_client([response])
        with pytest.raises(RateLimitError):
            client.quote_batch([_equity()])

    def test_depth_rate_limit_body_raises(self):
        """Depth wiring rejects failure bodies too."""
        response = {"data": {"805": "Too many requests."}, "_http_status": 429}
        client, _, _ = _make_client([response])
        with pytest.raises(RateLimitError):
            client.depth(_equity())

    def test_option_chain_rate_limit_body_raises(self):
        """Option chain must not silently return an empty chain on failure."""
        response = {"data": {"805": "Too many requests."}, "_http_status": 429}
        client, _, _ = _make_client([response])
        with pytest.raises(RateLimitError):
            client.get_option_chain(_index())

    def test_history_failure_body_raises(self):
        """History failure bodies raise instead of returning an empty series."""
        response = {"data": {"805": "Too many requests."}, "_http_status": 429}
        client, _, _ = _make_client([response])
        with pytest.raises(RateLimitError):
            client.history(
                _equity(),
                Timeframe.M5,
                datetime(2026, 8, 1, tzinfo=UTC),
                datetime(2026, 8, 5, tzinfo=UTC),
            )

    def test_get_quote(self):
        response = {
            "data": {
                "NSE_EQ": {
                    "2885": {
                        "last_price": 2500.0,
                        "volume": 100000,
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
        }
        client, _, _ = _make_client([response])
        quote = client.get_quote(_equity())
        assert isinstance(quote, Quote)
        assert quote.ltp.value == Decimal("2500")
        assert quote.volume.value == 100000

    def test_depth(self):
        response = {
            "data": {
                "NSE_EQ": {
                    "2885": {
                        "depth": {
                            "buy": [{"price": 2499.0, "quantity": 100}],
                            "sell": [{"price": 2501.0, "quantity": 50}],
                        }
                    }
                }
            }
        }
        client, _, _ = _make_client([response])
        d = client.depth(_equity())
        assert isinstance(d, Depth)
        assert len(d.bids) == 1
        assert len(d.asks) == 1

    def test_ltp_batch(self):
        response = {"data": {"NSE_EQ": {"2885": {"ltp": 2500.0}}}}
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
        response = {
            "data": {
                "NSE_EQ": {
                    "2885": {
                        "last_price": 2500.0,
                        "volume": 0,
                        "ohlc": {"open": 0, "high": 0, "low": 0, "close": 0},
                    }
                }
            }
        }
        client, _, _ = _make_client([response])
        result = client.quote_batch([_equity()])
        iid = InstrumentId.equity("NSE", "RELIANCE")
        assert iid in result

    def test_history_intraday_same_day_future_end_is_capped_at_now(self, monkeypatch):
        from tradex_brokers.dhan import _marketdata

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return cls(2026, 8, 5, 12, 30, tzinfo=tz)

        monkeypatch.setattr(_marketdata, "datetime", FrozenDateTime)
        client, fake, _ = _make_client([{"data": {}}])
        start = datetime(2026, 8, 5, 9, 15, tzinfo=UTC)
        end = datetime(2026, 8, 5, 15, 30, tzinfo=UTC)

        client.history(_equity(), Timeframe.M1, start, end)

        assert fake.last_payload("json")["toDate"] == "2026-08-05 12:30:00"

    def test_history_intraday_from_date_precedes_session_open(self):
        """Dhan's ``fromDate`` is exclusive — asking from 09:15 loses the open.

        Regression: a full-day request for 09:15-15:30 came back 09:16-15:14,
        so every stored session silently lost its first bar while the
        response still looked complete (non-empty).  The window must start one
        bar *before* the session open.
        """
        client, fake, _ = _make_client([{"data": {}}])
        start = datetime(2026, 8, 5, 9, 15, tzinfo=UTC)
        end = datetime(2026, 8, 5, 15, 30, tzinfo=UTC)

        client.history(_equity(), Timeframe.M1, start, end)

        payload = fake.last_payload("json")
        assert payload["fromDate"] == "2026-08-05 09:14:00"
        assert payload["interval"] == "1"

    def test_history_daily_uses_date_only_window(self):
        """Daily bars take /charts/historical, whose fromDate is a plain date.

        The one-bar lead above is an intraday concern only: a D1 request is
        day-granular, so the lead is capped at an hour and never reaches the
        previous session.
        """
        client, fake, _ = _make_client([{"data": {}}])
        start = datetime(2026, 8, 5, tzinfo=UTC)
        end = datetime(2026, 8, 5, tzinfo=UTC)

        client.history(_equity(), Timeframe.D1, start, end)

        payload = fake.last_payload("json")
        assert payload["fromDate"] == "2026-08-05"
        assert payload["toDate"] == "2026-08-05"

    def test_history_daily(self):
        response = {
            "data": {
                "open": [100.0, 101.0],
                "high": [105.0, 106.0],
                "low": [99.0, 100.0],
                "close": [104.0, 105.0],
                "volume": [1000, 1200],
                "timestamp": ["2026-08-01", "2026-08-02"],
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
                _equity(), "1w",
                datetime(2026, 8, 1, tzinfo=UTC),
                datetime(2026, 8, 5, tzinfo=UTC),
            )


# ---------------------------------------------------------------------------
# Option chain tests
# ---------------------------------------------------------------------------


class TestOptionChain:
    """Dhan /optionchain mapping (expiry list + per-expiry chain)."""

    def _chain_response(self, strikes: list[dict]) -> dict:
        """A Dhan /optionchain body with the given ``oc`` strike legs."""
        return {
            "data": {
                "last_price": 24000.0,
                "oc": {
                    str(strike["strike"]): {"ce": strike["ce"], "pe": strike["pe"]}
                    for strike in strikes
                },
            }
        }

    def _leg(self, security_id: object) -> dict:
        return {
            "security_id": security_id,
            "last_price": 0,
            "oi": 0,
            "volume": 0,
        }

    def _expiry_list_response(self) -> dict:
        return {"data": ["2026-12-29"]}

    def test_chain_with_placeholder_zero_legs_skipped(self):
        """Deep-ITM strikes with ``security_id == 0`` are not registered.

        Regression: Dhan returns placeholder legs (security_id 0) for
        non-traded far-expiry strikes; ``str(0)`` == "0" collided in the
        registry when the next placeholder strike registered the same key.
        """
        responses = [
            self._expiry_list_response(),
            self._chain_response([
                {"strike": 13000, "ce": self._leg(0), "pe": self._leg(0)},
                {"strike": 14000, "ce": self._leg(0), "pe": self._leg(0)},
                {"strike": 23000, "ce": self._leg(55001), "pe": self._leg(55002)},
                {"strike": 25000, "ce": self._leg(55003), "pe": self._leg(55004)},
            ]),
        ]
        client, _, registry = _make_client(responses)
        chain = client.get_option_chain(_index())
        expiries = chain.expiries()
        assert len(expiries) == 1
        pairs = list(expiries[0].pairs)
        # Only the real contracts (truthy security id) are paired.
        assert [p.strike.value for p in pairs] == [Decimal("23000"), Decimal("25000")]
        # The real legs are registered under their native security ids.
        assert registry.resolve("55001") is not None
        assert registry.resolve("55003") is not None
        # No placeholder "0" key was registered.
        assert registry.resolve("0") is None

    def test_chain_maps_all_strikes_with_real_ids(self):
        responses = [
            self._expiry_list_response(),
            self._chain_response([
                {"strike": 23000, "ce": self._leg(55001), "pe": self._leg(55002)},
                {"strike": 25000, "ce": self._leg(55003), "pe": self._leg(55004)},
            ]),
        ]
        client, _, registry = _make_client(responses)
        chain = client.get_option_chain(_index())
        pairs = list(chain.expiries()[0].pairs)
        assert len(pairs) == 2
        assert [p.strike.value for p in pairs] == [Decimal("23000"), Decimal("25000")]
        assert registry.resolve("55001") is not None
        assert registry.resolve("55004") is not None

    def test_chain_mcx_legs_built_on_mcx_exchange(self):
        """MCX /optionchain legs are built on the MCX exchange (not NFO).

        Regression: ``_option_exchange`` returned NFO for every non-BSE
        underlying, so MCX legs were mapped as NFO contracts — quote/order
        lookups on those legs would hit the wrong segment.
        """
        from datetime import date

        fut = Future.of("MCX", "CRUDEOIL", date(2026, 8, 19))
        responses = [
            {"data": ["2026-08-17"]},
            self._chain_response([
                {"strike": 10000, "ce": self._leg(573528), "pe": self._leg(573687)},
            ]),
        ]
        client, _, registry = _make_client(responses)
        registry.register(fut.instrument_id, {"key": "MCX:560977", "asset_class": "FUTURE"})
        chain = client.get_option_chain(fut)
        expiries = chain.expiries()
        assert len(expiries) == 1
        pair = list(expiries[0].pairs)[0]
        assert pair.call.instrument_id.exchange == "MCX"
        assert pair.put.instrument_id.exchange == "MCX"
        assert pair.call.instrument_id.underlying == "CRUDEOIL"
        assert pair.call.instrument_id.right == "CE"
        # Legs are registered under their native MCX security ids.
        assert registry.resolve("573528") == pair.call.instrument_id
        assert registry.resolve("573687") == pair.put.instrument_id


# ---------------------------------------------------------------------------
# Position / portfolio tests
# ---------------------------------------------------------------------------


class TestPositions:
    def test_get_positions(self):
        response = {
            "data": [
                {
                    "securityId": "2885",
                    "netQty": 10,
                    "avgCostPrice": 2500.0,
                    "realizedProfit": 100.0,
                    "unrealizedProfit": 50.0,
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

    def test_exit_all(self):
        response = {"data": {"status": "success"}}
        client, http, _ = _make_client([response])
        result = client.exit_all()
        assert result["status"] == "success"


# ---------------------------------------------------------------------------
# eDIS / kill switch tests
# ---------------------------------------------------------------------------


class TestEdisKillSwitch:
    def test_kill_switch(self):
        response = {"data": {"status": "ACTIVATED"}}
        client, _, _ = _make_client([response])
        result = client.kill_switch(enable=True)
        assert result["status"] == "ACTIVATED"

    def test_status_kill_switch(self):
        response = {"data": {"status": "ACTIVE"}}
        client, _, _ = _make_client([response])
        result = client.status_kill_switch()
        assert result["status"] == "ACTIVE"

    def test_edis_status(self):
        response = {"data": {"authorized": True}}
        client, _, _ = _make_client([response])
        result = client.edis_status("INE002A01018")
        assert result["authorized"] is True

    def test_authorize_edis(self):
        response = {"data": {"status": "authorized"}}
        client, _, _ = _make_client([response])
        result = client.authorize_edis("INE002A01018", 10, "NSE")
        assert result["status"] == "authorized"


# ---------------------------------------------------------------------------
# Trade tests
# ---------------------------------------------------------------------------


class TestTrades:
    def test_trade_book(self):
        response = {"data": [{"tradeId": "T1", "orderId": "O1"}]}
        client, _, _ = _make_client([response])
        result = client.trade_book()
        assert len(result) == 1

    def test_get_trade_history(self):
        response = {"data": [{"tradeId": "T1"}]}
        client, _, _ = _make_client([response])
        result = client.get_trade_history()
        assert len(result) == 1

    def test_get_order_by_correlation_id(self):
        response = {"data": {"orderId": "O1", "correlationId": "abc"}}
        client, _, _ = _make_client([response])
        result = client.get_order_by_correlation_id("abc")
        assert result["orderId"] == "O1"


# ---------------------------------------------------------------------------
# Streaming tests
# ---------------------------------------------------------------------------


class TestStreaming:
    def test_order_stream_backend(self):
        client, _, _ = _make_client()
        backend = client.order_stream_backend()
        assert backend is not None

    def test_market_stream_backend(self):
        client, _, _ = _make_client()
        backend = client.market_stream_backend()
        assert backend is not None

    def test_depth_stream_backend(self):
        client, _, _ = _make_client()
        backend = client.depth_stream_backend()
        assert backend is not None

    def test_invalidate_read_cache(self):
        client, http, _ = _make_client()
        client.invalidate_read_cache()


class TestStreamOrderFromRow:
    """Live order-update row mapping used by the LiveFillBridge (HIGH-4)."""

    def _row(self, **overrides: Any) -> dict[str, Any]:
        row: dict[str, Any] = {
            "orderId": "dhan-order-1",
            "securityId": "2885",
            "transactionType": "BUY",
            "orderType": "MARKET",
            "quantity": "10",
            "price": "2500",
            "orderStatus": "TRADED",
            "filledQty": "10",
            "correlationId": "11111111-2222-3333-4444-555555555555",
        }
        row.update(overrides)
        return row

    def test_traded_row_uses_traded_price_for_fill(self) -> None:
        """Fills trade at ``tradedPrice``, not the order's limit price."""
        client, _, _ = _make_client()
        order = client._stream_order_from_row(
            self._row(tradedPrice="2505.55")
        )
        assert order.status is OrderStatus.FILLED
        assert order.price is not None
        assert order.price.value == Decimal("2505.55")
        assert order.filled_quantity.value == Decimal("10")
        assert order.correlation_id.value == UUID("11111111-2222-3333-4444-555555555555")
        assert order.instrument.instrument_id == InstrumentId.equity("NSE", "RELIANCE")

    def test_part_traded_keeps_limit_price_without_traded_price(self) -> None:
        """No ``tradedPrice`` → fall back to the order price (limit)."""
        client, _, _ = _make_client()
        order = client._stream_order_from_row(
            self._row(orderStatus="PART_TRADED", filledQty="3")
        )
        assert order.status is OrderStatus.PARTIALLY_FILLED
        assert order.price is not None
        assert order.price.value == Decimal("2500")
        assert order.filled_quantity.value == Decimal("3")

    def test_pending_row_maps_status_without_fill(self) -> None:
        client, _, _ = _make_client()
        order = client._stream_order_from_row(
            self._row(orderStatus="PENDING", filledQty="0")
        )
        assert order.status is OrderStatus.PENDING
        assert order.filled_quantity.value == Decimal("0")

    def test_unmapped_security_raises(self) -> None:
        client, _, _ = _make_client()
        with pytest.raises(ValueError, match="securityId"):
            client._stream_order_from_row(self._row(securityId="999999"))

    def test_non_uuid_correlation_id_preserved_verbatim(self) -> None:
        """Strategy-bridge ids (``strat-...``) survive the echo round-trip so
        the live-fill bridge can match the engine order by correlation id."""
        client, _, _ = _make_client()
        order = client._stream_order_from_row(
            self._row(correlationId="strat-deadbeef")
        )
        assert order.correlation_id.value == "strat-deadbeef"


def test_dhan_api_client_exposes_rate_limiter():
    """DhanApiClient.rate_limiter is the same instance its HTTP client holds."""
    limiter = MultiBucketRateLimiter(default=RateLimitConfig())
    http = MagicMock()
    http.rate_limiter = limiter
    client = DhanApiClient(http=http, registry=MagicMock())

    assert client.rate_limiter is limiter
