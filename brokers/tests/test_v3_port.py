"""Tests for ported v3 broker gaps.

Covers: depth_parser, instruments, provider_common, paper, ws_streams.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import pytest
from tradex_domain.enums import (
    OrderSide,
    OrderStatus,
    OrderType,
    TimeInForce,
)
from tradex_domain.execution import Order, OrderRequest
from tradex_domain.instruments import Equity, Future, Index, Instrument, Option
from tradex_domain.market import Quote
from tradex_domain.value_objects import (
    InstrumentId,
    OrderId,
    Price,
    Quantity,
)
from tradex_domain.wire import InstrumentRegistry

# ---------------------------------------------------------------------------
# Task 2: dhan/depth_parser
# ---------------------------------------------------------------------------


class TestDhanDepthParser:
    """Tests for parse_depth_frame and depth_frame_to_depth."""

    def _make_bid_packet(self, security_id: int, price: float, qty: int) -> bytes:
        """Build a minimal depth-20 bid sub-packet."""
        header = bytearray(12)
        header[2] = 41  # BID_RESPONSE_CODE
        struct.pack_into("<I", header, 4, security_id)
        # One level
        level = bytearray(16)
        struct.pack_into("<d", level, 0, price)
        struct.pack_into("<I", level, 8, qty)
        # Pad remaining 19 slots
        padding = bytearray(19 * 16)
        return bytes(header) + bytes(level) + bytes(padding)

    def _make_ask_packet(self, security_id: int, price: float, qty: int) -> bytes:
        header = bytearray(12)
        header[2] = 51  # ASK_RESPONSE_CODE
        struct.pack_into("<I", header, 4, security_id)
        level = bytearray(16)
        struct.pack_into("<d", level, 0, price)
        struct.pack_into("<I", level, 8, qty)
        padding = bytearray(19 * 16)
        return bytes(header) + bytes(level) + bytes(padding)

    def test_parse_empty_frame(self) -> None:
        from tradex_brokers.dhan.depth_parser import parse_depth_frame

        assert parse_depth_frame(b"") == []
        assert parse_depth_frame(b"\x00" * 5) == []

    def test_parse_bid_packet(self) -> None:
        from tradex_brokers.dhan.depth_parser import parse_depth_frame

        packet = self._make_bid_packet(2885, 100.50, 500)
        result = parse_depth_frame(packet, total_slots=20)
        assert len(result) == 1
        assert result[0]["side"] == "bids"
        assert result[0]["security_id"] == 2885
        assert len(result[0]["levels"]) == 1
        price, qty = result[0]["levels"][0]
        assert price.value == Decimal("100.5")
        assert qty.value == Decimal("500")

    def test_parse_ask_packet(self) -> None:
        from tradex_brokers.dhan.depth_parser import parse_depth_frame

        packet = self._make_ask_packet(1234, 200.75, 1000)
        result = parse_depth_frame(packet, total_slots=20)
        assert len(result) == 1
        assert result[0]["side"] == "asks"
        assert result[0]["security_id"] == 1234

    def test_parse_combined_frame(self) -> None:
        from tradex_brokers.dhan.depth_parser import parse_depth_frame

        bid = self._make_bid_packet(2885, 100.0, 500)
        ask = self._make_ask_packet(2885, 101.0, 300)
        combined = bid + ask
        result = parse_depth_frame(combined, total_slots=20)
        assert len(result) == 2
        assert result[0]["side"] == "bids"
        assert result[1]["side"] == "asks"

    def test_depth_frame_to_depth(self) -> None:
        from tradex_brokers.dhan.depth_parser import depth_frame_to_depth

        instrument = Equity.of("NSE", "RELIANCE")
        bid = self._make_bid_packet(2885, 100.0, 500)
        ask = self._make_ask_packet(2885, 101.0, 300)
        combined = bid + ask
        depth = depth_frame_to_depth(combined, instrument, total_slots=20)
        assert depth is not None
        assert depth.instrument == instrument
        assert len(depth.bids) == 1
        assert len(depth.asks) == 1
        assert depth.timestamp is not None

    def test_depth_frame_to_depth_empty(self) -> None:
        from tradex_brokers.dhan.depth_parser import depth_frame_to_depth

        instrument = Equity.of("NSE", "RELIANCE")
        assert depth_frame_to_depth(b"", instrument) is None

    def test_depth_200_no_security_id(self) -> None:
        from tradex_brokers.dhan.depth_parser import parse_depth_frame

        # depth-200: header does NOT carry security_id
        header = bytearray(12)
        header[2] = 41  # bid
        level = bytearray(16)
        struct.pack_into("<d", level, 0, 99.5)
        struct.pack_into("<I", level, 8, 100)
        padding = bytearray(19 * 16)
        packet = bytes(header) + bytes(level) + bytes(padding)
        result = parse_depth_frame(
            packet, total_slots=20, header_carries_security_id=False, security_id=42
        )
        assert len(result) == 1
        assert result[0]["security_id"] == 42


# ---------------------------------------------------------------------------
# Task 3: dhan/instruments helpers
# ---------------------------------------------------------------------------


class TestDhanInstrumentsHelpers:
    """Tests for _parse_expiry, _value, load_mcx_rows."""

    def test_value_first_match(self) -> None:
        from tradex_brokers.dhan.instruments import _value

        row = {"a": "hello", "b": "world"}
        assert _value(row, "a", "b") == "hello"

    def test_value_fallback(self) -> None:
        from tradex_brokers.dhan.instruments import _value

        row = {"b": "world"}
        assert _value(row, "a", "b") == "world"

    def test_value_empty(self) -> None:
        from tradex_brokers.dhan.instruments import _value

        assert _value({}, "a") == ""
        assert _value({"a": "  "}, "a") == ""

    def test_parse_expiry_iso(self) -> None:
        from tradex_brokers.dhan.instruments import _parse_expiry

        assert _parse_expiry("2024-08-28") == date(2024, 8, 28)

    def test_parse_expiry_compact(self) -> None:
        from tradex_brokers.dhan.instruments import _parse_expiry

        assert _parse_expiry("20240828") == date(2024, 8, 28)

    def test_parse_expiry_dd_mm_yyyy(self) -> None:
        from tradex_brokers.dhan.instruments import _parse_expiry

        assert _parse_expiry("28-08-2024") == date(2024, 8, 28)

    def test_parse_expiry_with_time(self) -> None:
        from tradex_brokers.dhan.instruments import _parse_expiry

        assert _parse_expiry("2024-08-28 14:30:00") == date(2024, 8, 28)

    def test_parse_expiry_invalid(self) -> None:
        from tradex_brokers.dhan.instruments import _parse_expiry

        with pytest.raises(ValueError):
            _parse_expiry("not-a-date")

    def test_load_mcx_rows_future(self) -> None:
        from tradex_brokers.dhan.instruments import load_mcx_rows

        rows = [
            {
                "Symbol": "CRUDEOIL",
                "Expiry": "2024-12-19",
                "SecurityId": "12345",
            }
        ]
        registry = InstrumentRegistry()
        instruments = load_mcx_rows(rows, registry)
        assert len(instruments) == 1
        assert isinstance(instruments[0], Future)
        assert instruments[0].exchange.value == "MCX"

    def test_load_mcx_rows_option(self) -> None:
        from tradex_brokers.dhan.instruments import load_mcx_rows

        rows = [
            {
                "Symbol": "CRUDEOIL",
                "Expiry": "2024-12-19",
                "StrikePrice": "5000",
                "OptionType": "CE",
                "SecurityId": "12346",
            }
        ]
        registry = InstrumentRegistry()
        instruments = load_mcx_rows(rows, registry)
        assert len(instruments) == 1
        assert isinstance(instruments[0], Option)
        assert instruments[0].strike == Decimal("5000")

    def test_load_mcx_rows_skips_invalid(self) -> None:
        from tradex_brokers.dhan.instruments import load_mcx_rows

        rows = [{"Symbol": "", "Expiry": ""}]
        registry = InstrumentRegistry()
        assert load_mcx_rows(rows, registry) == []


# ---------------------------------------------------------------------------
# Task 5: upstox/ws_decoder (smoke test — protobuf may not be installed)
# ---------------------------------------------------------------------------


class TestUpstoxWsDecoder:
    """Smoke tests for ws_decoder helpers."""

    def test_epoch_ms_to_iso(self) -> None:
        from tradex_brokers.upstox.ws_decoder import _epoch_ms_to_iso

        result = _epoch_ms_to_iso(1704067200000)
        assert "2024-01-01" in result

    def test_shape_ltpc(self) -> None:
        from tradex_brokers.upstox.ws_decoder import _shape_ltpc

        ltpc = MagicMock()
        ltpc.ltp = 100.5
        result = _shape_ltpc(ltpc, "2024-01-01T00:00:00+00:00")
        assert result["last_price"] == 100.5
        assert result["depth"] == {"buy": [], "sell": []}

    def test_shape_depth_rows(self) -> None:
        from tradex_brokers.upstox.ws_decoder import _shape_depth_rows

        row = MagicMock()
        row.bidP = 100.0
        row.bidQ = 500
        row.askP = 101.0
        row.askQ = 300
        result = _shape_depth_rows([row])
        assert len(result["buy"]) == 1
        assert len(result["sell"]) == 1
        assert result["buy"][0]["price"] == 100.0

    def test_shape_depth_rows_zero_qty(self) -> None:
        from tradex_brokers.upstox.ws_decoder import _shape_depth_rows

        row = MagicMock()
        row.bidP = 100.0
        row.bidQ = 0
        row.askP = 101.0
        row.askQ = 0
        result = _shape_depth_rows([row])
        assert result["buy"] == []
        assert result["sell"] == []

    def test_shape_market_full_carries_greeks(self) -> None:
        """Market-full frames with optionGreeks surface a ``greeks`` row key."""
        from tradex_brokers.proto.MarketDataFeed_pb2 import (
            MarketFullFeed,  # type: ignore[attr-defined]
        )
        from tradex_brokers.upstox.ws_decoder import _shape_market_full

        mf = MarketFullFeed()
        mf.ltpc.ltp = 100.5
        mf.optionGreeks.delta = 0.62
        mf.optionGreeks.gamma = 0.0012
        mf.optionGreeks.theta = -4.5
        mf.optionGreeks.vega = 7.8
        mf.optionGreeks.rho = 0.02
        row = _shape_market_full(mf, "2024-01-01T00:00:00+00:00")
        assert row["greeks"] == {
            "delta": 0.62, "gamma": 0.0012, "theta": -4.5, "vega": 7.8, "rho": 0.02,
        }

    def test_shape_market_full_no_greeks_key(self) -> None:
        """Market-full frames without optionGreeks omit the ``greeks`` key."""
        from tradex_brokers.proto.MarketDataFeed_pb2 import (
            MarketFullFeed,  # type: ignore[attr-defined]
        )
        from tradex_brokers.upstox.ws_decoder import _shape_market_full

        mf = MarketFullFeed()
        mf.ltpc.ltp = 100.5
        row = _shape_market_full(mf, "2024-01-01T00:00:00+00:00")
        assert "greeks" not in row

    def test_shape_first_level_carries_greeks(self) -> None:
        """First-level-with-greeks frames surface the greeks row key too."""
        from tradex_brokers.proto.MarketDataFeed_pb2 import (
            FirstLevelWithGreeks,  # type: ignore[attr-defined]
        )
        from tradex_brokers.upstox.ws_decoder import _shape_first_level

        fl = FirstLevelWithGreeks()
        fl.ltpc.ltp = 2500.0
        fl.optionGreeks.delta = -0.38
        row = _shape_first_level(fl, "2024-01-01T00:00:00+00:00")
        assert row["greeks"]["delta"] == -0.38


# ---------------------------------------------------------------------------
# Task 7: provider_common
# ---------------------------------------------------------------------------


class TestProviderCommon:
    """Tests for provider_common helpers."""

    def test_is_token_rejection_response_401(self) -> None:
        from tradex_brokers.common.provider_common import is_token_rejection_response

        assert is_token_rejection_response(401, {}) is True

    def test_is_token_rejection_response_403(self) -> None:
        from tradex_brokers.common.provider_common import is_token_rejection_response

        assert is_token_rejection_response(403, {}) is True

    def test_is_token_rejection_response_400_with_marker(self) -> None:
        from tradex_brokers.common.provider_common import is_token_rejection_response

        assert is_token_rejection_response(400, "DH-901 invalid token") is True

    def test_is_token_rejection_response_400_no_marker(self) -> None:
        from tradex_brokers.common.provider_common import is_token_rejection_response

        assert is_token_rejection_response(400, "bad request") is False

    def test_is_token_rejection_error(self) -> None:
        from tradex_brokers.common.provider_common import is_token_rejection_error

        assert is_token_rejection_error(Exception("failed with HTTP 401")) is True
        assert is_token_rejection_error(Exception("DH-906")) is True
        assert is_token_rejection_error(Exception("not found")) is False

    def test_as_price(self) -> None:
        from tradex_brokers.common.provider_common import as_price

        p = as_price("100.50")
        assert p.value == Decimal("100.50")

    def test_as_price_default(self) -> None:
        from tradex_brokers.common.provider_common import as_price

        p = as_price(None)
        assert p.value == Decimal("0")

    def test_parse_date(self) -> None:
        from tradex_brokers.common.provider_common import parse_date

        assert parse_date("2024-08-28") == date(2024, 8, 28)
        assert parse_date(None) is None
        assert parse_date("") is None

    def test_first_mapping_dict(self) -> None:
        from tradex_brokers.common.provider_common import first_mapping

        assert first_mapping({"a": 1}) == {"a": 1}

    def test_first_mapping_list(self) -> None:
        from tradex_brokers.common.provider_common import first_mapping

        assert first_mapping([{"a": 1}]) == {"a": 1}

    def test_first_mapping_empty(self) -> None:
        from tradex_brokers.common.provider_common import first_mapping

        assert first_mapping([]) == {}

    def test_unwrap_data(self) -> None:
        from tradex_brokers.common.provider_common import unwrap_data

        assert unwrap_data({"data": {"x": 1}}) == {"x": 1}
        assert unwrap_data({"x": 1}) == {"x": 1}

    def test_provider_key_found(self) -> None:
        from tradex_brokers.common.provider_common import provider_key

        registry = InstrumentRegistry()
        iid = InstrumentId.equity("NSE", "RELIANCE")
        registry.register(iid, {"key": "NSE:RELIANCE"})
        assert provider_key(registry, iid) == "NSE:RELIANCE"

    def test_provider_key_not_found(self) -> None:
        from tradex_domain import InstrumentNotFoundError

        from tradex_brokers.common.provider_common import provider_key

        registry = InstrumentRegistry()
        iid = InstrumentId.equity("NSE", "RELIANCE")
        with pytest.raises(InstrumentNotFoundError):
            provider_key(registry, iid)

    def test_instrument_from_id_equity(self) -> None:
        from tradex_brokers.common.provider_common import instrument_from_id

        iid = InstrumentId.equity("NSE", "RELIANCE")
        inst = instrument_from_id(iid)
        assert isinstance(inst, Equity)

    def test_instrument_from_id_future(self) -> None:
        from tradex_brokers.common.provider_common import instrument_from_id

        iid = InstrumentId.future("NFO", "NIFTY", date(2024, 12, 26))
        inst = instrument_from_id(iid)
        assert isinstance(inst, Future)

    def test_instrument_from_id_option(self) -> None:
        from tradex_brokers.common.provider_common import instrument_from_id

        iid = InstrumentId.option("NFO", "NIFTY", date(2024, 12, 26), 20000, "CE")
        inst = instrument_from_id(iid)
        assert isinstance(inst, Option)

    def test_instrument_from_id_index(self) -> None:
        from tradex_brokers.common.provider_common import instrument_from_id

        iid = InstrumentId.equity("IDX", "NIFTY")
        inst = instrument_from_id(iid)
        assert isinstance(inst, Index)

    def test_resolve_instrument(self) -> None:
        from tradex_brokers.common.provider_common import resolve_instrument

        registry = InstrumentRegistry()
        iid = InstrumentId.equity("NSE", "RELIANCE")
        registry.register(iid, {"key": "RELIANCE"})
        registry.add_alias("RELIANCE", iid)
        inst = resolve_instrument(registry, "RELIANCE")
        assert isinstance(inst, Equity)

    def test_resolve_instrument_not_found(self) -> None:
        from tradex_domain import InstrumentNotFoundError

        from tradex_brokers.common.provider_common import resolve_instrument

        registry = InstrumentRegistry()
        with pytest.raises(InstrumentNotFoundError):
            resolve_instrument(registry, "UNKNOWN")

    def test_future_chain_from_master(self) -> None:
        from tradex_brokers.common.provider_common import future_chain_from_master

        underlying = Equity.of("NFO", "NIFTY")
        f1 = Future.of("NFO", "NIFTY", date(2024, 12, 26))
        f2 = Future.of("NFO", "NIFTY", date(2025, 1, 30))
        eq = Equity.of("NSE", "RELIANCE")
        result = future_chain_from_master([f2, f1, eq], underlying)
        assert len(result) == 2
        assert result[0].expiry == date(2024, 12, 26)  # sorted ascending

    def test_future_chain_from_master_scopes_exchange(self) -> None:
        """NSE_COM SILVER futures never leak into an MCX SILVER chain."""
        from tradex_brokers.common.provider_common import future_chain_from_master

        mcx = Future.of("MCX", "SILVER", date(2026, 9, 4))
        nse_com = Future.of("NSE_COMM", "SILVER", date(2026, 9, 4))
        result = future_chain_from_master([mcx, nse_com], Equity.of("MCX", "SILVER"))
        assert len(result) == 1
        assert result[0].instrument_id.exchange == "MCX"

    def test_future_chain_from_master_index_underlying_spans_exchanges(self) -> None:
        """An IDX underlying matches derivatives on product exchanges (NFO)."""
        from tradex_brokers.common.provider_common import future_chain_from_master

        nfo = Future.of("NFO", "NIFTY", date(2024, 12, 26))
        result = future_chain_from_master([nfo], Index.of("IDX", "NIFTY"))
        assert len(result) == 1

    def test_build_instrument_from_row_equity(self) -> None:
        from tradex_brokers.common.provider_common import build_instrument_from_row

        inst = build_instrument_from_row({"exchange": "NSE", "symbol": "RELIANCE"})
        assert isinstance(inst, Equity)

    def test_build_instrument_from_row_option(self) -> None:
        from tradex_brokers.common.provider_common import build_instrument_from_row

        inst = build_instrument_from_row({
            "exchange": "MCX", "symbol": "SILVERM-24Aug2026-279000-CE",
            "right": "CE", "expiry": "2026-08-24", "strike": "279000.00000",
            "underlying": "SILVERM",
        })
        assert isinstance(inst, Option)
        assert inst.instrument_id.expiry == date(2026, 8, 24)
        assert inst.instrument_id.strike == Decimal("279000")
        assert inst.instrument_id.right == "CE"

    def test_build_instrument_from_row_future(self) -> None:
        from tradex_brokers.common.provider_common import build_instrument_from_row

        inst = build_instrument_from_row({
            "exchange": "MCX", "symbol": "SILVER-04Sep2026-FUT",
            "instrument_type": "FUTCOM", "expiry": "2026-09-04",
            "underlying": "SILVER",
        })
        assert isinstance(inst, Future)
        assert inst.instrument_id.expiry == date(2026, 9, 4)

    def test_build_instrument_from_row_malformed_derivative_falls_back(self) -> None:
        """A row with a broken expiry/strike degrades to Equity, never crashes."""
        from tradex_brokers.common.provider_common import build_instrument_from_row

        inst = build_instrument_from_row({
            "exchange": "MCX", "symbol": "GOLD-04Sep2026-FUT",
            "instrument_type": "FUTCOM", "expiry": "not-a-date", "underlying": "GOLD",
        })
        assert isinstance(inst, Equity)
        inst2 = build_instrument_from_row({
            "exchange": "MCX", "symbol": "GOLD-04Sep2026-CE",
            "right": "CE", "expiry": "2026-09-04", "strike": "abc", "underlying": "GOLD",
        })
        assert isinstance(inst2, Equity)

    def test_option_chain_from_master(self) -> None:
        from tradex_brokers.common.provider_common import option_chain_from_master

        underlying = Equity.of("MCX", "SILVERM")
        c1 = Option.of("MCX", "SILVERM", date(2026, 8, 24), 279000, "CE")
        p1 = Option.of("MCX", "SILVERM", date(2026, 8, 24), 279000, "PE")
        c2 = Option.of("MCX", "SILVERM", date(2026, 8, 24), 280000, "CE")
        p2 = Option.of("MCX", "SILVERM", date(2026, 8, 24), 280000, "PE")
        c3 = Option.of("MCX", "SILVERM", date(2026, 9, 24), 280000, "CE")
        p3 = Option.of("MCX", "SILVERM", date(2026, 9, 24), 280000, "PE")
        gold = Option.of("MCX", "GOLD", date(2026, 8, 24), 100000, "CE")
        chain = option_chain_from_master([c1, p1, c2, p2, c3, p3, gold], underlying)
        expiries = chain.expiries()
        assert [e.expiry_date for e in expiries] == [date(2026, 8, 24), date(2026, 9, 24)]
        assert [p.strike.value for p in expiries[0].pairs] == [Decimal("279000"), Decimal("280000")]
        assert expiries[0].pairs[0].call.right == "CE"
        assert expiries[0].pairs[0].put.right == "PE"

    def test_option_chain_from_master_scopes_exchange(self) -> None:
        """Same-root options on other exchanges never pollute the MCX chain."""
        from tradex_brokers.common.provider_common import option_chain_from_master

        mcx_ce = Option.of("MCX", "SILVER", date(2026, 8, 24), 90000, "CE")
        mcx_pe = Option.of("MCX", "SILVER", date(2026, 8, 24), 90000, "PE")
        nse_ce = Option.of("NSE_COMM", "SILVER", date(2026, 8, 24), 90000, "CE")
        nse_pe = Option.of("NSE_COMM", "SILVER", date(2026, 8, 24), 90000, "PE")
        chain = option_chain_from_master(
            [mcx_ce, mcx_pe, nse_ce, nse_pe], Equity.of("MCX", "SILVER")
        )
        assert len(chain.expiries()) == 1
        assert len(chain.expiries()[0].pairs) == 1
        assert chain.expiries()[0].pairs[0].call.instrument_id.exchange == "MCX"

    def test_option_chain_from_master_expiry_filter(self) -> None:
        from tradex_brokers.common.provider_common import option_chain_from_master

        underlying = Equity.of("MCX", "SILVERM")
        c1 = Option.of("MCX", "SILVERM", date(2026, 8, 24), 279000, "CE")
        p1 = Option.of("MCX", "SILVERM", date(2026, 8, 24), 279000, "PE")
        chain = option_chain_from_master([c1, p1], underlying, expiry="2026-08-24")
        assert len(chain.expiries()) == 1
        chain_none = option_chain_from_master([c1, p1], underlying, expiry="2027-01-01")
        assert chain_none.expiries() == []


# ---------------------------------------------------------------------------
# Task 6: paper/adapter
# ---------------------------------------------------------------------------


class TestPaperBroker:
    """Tests for the ported PaperBroker."""

    def _make_request(self, instrument: Instrument | None = None) -> OrderRequest:
        inst = instrument or Equity.of("NSE", "RELIANCE")
        return OrderRequest(
            instrument=inst,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(value=Decimal("10")),
        )

    def test_connect_on_init(self) -> None:
        from tradex_brokers.paper.adapter import PaperBroker

        broker = PaperBroker()
        assert broker._connected is True

    def test_submit_order_market(self) -> None:
        from tradex_brokers.paper.adapter import PaperBroker

        broker = PaperBroker(auto_fill=True)
        inst = Equity.of("NSE", "RELIANCE")
        broker.set_quote(inst, ltp=Price(value=Decimal("100")))
        oid = broker.submit_order(self._make_request(inst))
        order = broker.get_order(oid)
        assert order.status == OrderStatus.FILLED

    def test_submit_order_limit_fills_at_limit(self) -> None:
        from tradex_brokers.paper.adapter import PaperBroker

        broker = PaperBroker(auto_fill=True)
        req = OrderRequest(
            instrument=Equity.of("NSE", "RELIANCE"),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("100")),
        )
        oid = broker.submit_order(req)
        order = broker.get_order(oid)
        # LIMIT orders fill at their limit price even without a tape quote
        assert order.status == OrderStatus.FILLED
        assert order.price.value == Decimal("100")

    def test_set_quote_triggers_fill(self) -> None:
        from tradex_brokers.paper.adapter import PaperBroker

        broker = PaperBroker()
        inst = Equity.of("NSE", "RELIANCE")
        # Use a STOP order that needs a trigger price to be reached
        req = OrderRequest(
            instrument=inst,
            side=OrderSide.BUY,
            order_type=OrderType.STOP,
            quantity=Quantity(value=Decimal("10")),
            trigger_price=Price(value=Decimal("100")),
        )
        oid = broker.submit_order(req)
        # LTP not set, so stop not triggered
        assert broker.get_order(oid).status == OrderStatus.ACK
        broker.set_quote(inst, ltp=Price(value=Decimal("100")))
        assert broker.get_order(oid).status == OrderStatus.FILLED

    def test_cancel_order(self) -> None:
        from tradex_brokers.paper.adapter import PaperBroker

        broker = PaperBroker()
        inst = Equity.of("NSE", "RELIANCE")
        # Use a STOP order with high trigger so it stays in ACK
        req = OrderRequest(
            instrument=inst,
            side=OrderSide.BUY,
            order_type=OrderType.STOP,
            quantity=Quantity(value=Decimal("10")),
            trigger_price=Price(value=Decimal("200")),
        )
        oid = broker.submit_order(req)
        cancelled = broker.cancel_order(oid)
        assert cancelled.status == OrderStatus.CANCELLED

    def test_cash_ledger_buy(self) -> None:
        from tradex_brokers.paper.adapter import PaperBroker

        broker = PaperBroker(starting_cash=Decimal("100000"), auto_fill=True)
        inst = Equity.of("NSE", "RELIANCE")
        broker.set_quote(inst, ltp=Price(value=Decimal("100")))
        broker.submit_order(self._make_request(inst))
        acct = broker.get_account()
        assert acct.balance is not None
        assert acct.balance.amount == Decimal("99000")  # 10 * 100

    def test_cash_ledger_sell(self) -> None:
        from tradex_brokers.paper.adapter import PaperBroker

        broker = PaperBroker(starting_cash=Decimal("100000"), auto_fill=True)
        inst = Equity.of("NSE", "RELIANCE")
        broker.set_quote(inst, ltp=Price(value=Decimal("100")))
        req = OrderRequest(
            instrument=inst,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=Quantity(value=Decimal("10")),
        )
        broker.submit_order(req)
        acct = broker.get_account()
        assert acct.balance is not None
        assert acct.balance.amount == Decimal("101000")  # 100000 + 10 * 100

    def test_stop_order_trigger(self) -> None:
        from tradex_brokers.paper.adapter import PaperBroker

        broker = PaperBroker()
        inst = Equity.of("NSE", "RELIANCE")
        broker.set_quote(inst, ltp=Price(value=Decimal("100")))
        req = OrderRequest(
            instrument=inst,
            side=OrderSide.BUY,
            order_type=OrderType.STOP,
            quantity=Quantity(value=Decimal("10")),
            trigger_price=Price(value=Decimal("105")),
        )
        oid = broker.submit_order(req)
        # LTP=100 < trigger=105, so not filled yet
        assert broker.get_order(oid).status == OrderStatus.ACK
        # Set quote above trigger
        broker.set_quote(inst, ltp=Price(value=Decimal("106")))
        assert broker.get_order(oid).status == OrderStatus.FILLED

    def test_require_connected(self) -> None:
        from tradex_domain import BrokerUnavailableError

        from tradex_brokers.paper.adapter import PaperBroker

        broker = PaperBroker()
        broker.close()
        with pytest.raises(BrokerUnavailableError, match="not connected"):
            broker.submit_order(self._make_request())

    def test_synchronous_fill(self) -> None:
        from tradex_brokers.paper.adapter import PaperBroker

        broker = PaperBroker()
        assert broker.synchronous_fill is True

    def test_get_quote_unseeded(self) -> None:
        from tradex_brokers.paper.adapter import PaperBroker

        broker = PaperBroker()
        inst = Equity.of("NSE", "RELIANCE")
        # Unseeded instruments get a synthetic default quote
        quote = broker.get_quote(inst)
        assert quote.ltp.value > 0
        assert quote.instrument == inst

    def test_set_quote_bid_ask_mid(self) -> None:
        from tradex_brokers.paper.adapter import PaperBroker

        broker = PaperBroker()
        inst = Equity.of("NSE", "RELIANCE")
        broker.set_quote(inst, bid=Price(value=Decimal("99")), ask=Price(value=Decimal("101")))
        quote = broker.get_quote(inst)
        assert quote.ltp.value == Decimal("100")  # mid

    def test_search(self) -> None:
        from tradex_brokers.paper.adapter import PaperBroker

        broker = PaperBroker()
        inst = Equity.of("NSE", "RELIANCE")
        broker.set_quote(inst, ltp=Price(value=Decimal("100")))
        results = broker.search("REL")
        assert len(results) == 1
        assert results[0].symbol == "RELIANCE"


# ---------------------------------------------------------------------------
# Task 1: ws_streams (smoke tests — no real WS connections)
# ---------------------------------------------------------------------------


class TestDhanWsStreams:
    """Smoke tests for Dhan WS stream backends."""

    def test_dhan_order_stream_feed_raw(self) -> None:
        from tradex_brokers.dhan.ws_streams import DhanOrderStreamBackend

        received: list[Order] = []

        def map_order(row: Mapping[str, Any]) -> Order:
            return Order(
                order_id=OrderId(value=row.get("orderId", "test")),
                instrument=Equity.of("NSE", "RELIANCE"),
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Quantity(value=Decimal("10")),
                price=None,
                time_in_force=TimeInForce.DAY,
                status=OrderStatus.ACK,
            )

        backend = DhanOrderStreamBackend(
            token_provider=lambda: "token",
            client_id="client",
            map_order=map_order,
        )
        backend._order_handlers["test"] = received.append
        raw = json.dumps({"orderId": "123", "type": "order"})
        backend.feed_raw(raw)
        assert len(received) == 1

    def test_dhan_order_stream_feed_raw_list(self) -> None:
        from tradex_brokers.dhan.ws_streams import DhanOrderStreamBackend

        received: list[Order] = []

        def map_order(row: Mapping[str, Any]) -> Order:
            return Order(
                order_id=OrderId(value=row.get("orderId", "test")),
                instrument=Equity.of("NSE", "RELIANCE"),
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Quantity(value=Decimal("10")),
                price=None,
                time_in_force=TimeInForce.DAY,
                status=OrderStatus.ACK,
            )

        backend = DhanOrderStreamBackend(
            token_provider=lambda: "token",
            client_id="client",
            map_order=map_order,
        )
        backend._order_handlers["test"] = received.append
        raw = json.dumps([{"orderId": "123"}, {"orderId": "456"}])
        backend.feed_raw(raw)
        assert len(received) == 2

    def test_dhan_order_stream_skips_non_order(self) -> None:
        from tradex_brokers.dhan.ws_streams import DhanOrderStreamBackend

        received: list[Order] = []

        def map_order(row: Mapping[str, Any]) -> Order:
            return Order(
                order_id=OrderId(value="test"),
                instrument=Equity.of("NSE", "RELIANCE"),
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Quantity(value=Decimal("10")),
                price=None,
                time_in_force=TimeInForce.DAY,
                status=OrderStatus.ACK,
            )

        backend = DhanOrderStreamBackend(
            token_provider=lambda: "token",
            client_id="client",
            map_order=map_order,
        )
        backend._order_handlers["test"] = received.append
        raw = json.dumps({"type": "trade", "data": {}})
        backend.feed_raw(raw)
        assert len(received) == 0

    def test_dhan_market_stream_feed_raw(self) -> None:
        from tradex_brokers.dhan.ws_streams import DhanMarketDataStreamBackend

        received: list[Quote] = []
        registry = InstrumentRegistry()
        iid = InstrumentId.equity("NSE", "RELIANCE")
        registry.register(iid, {"key": "2885"})
        registry.add_alias("2885", iid)

        backend = DhanMarketDataStreamBackend(
            token_provider=lambda: "token",
            client_id="client",
            registry=registry,
        )
        backend._quote_handlers["test"] = received.append
        raw = json.dumps({
            "segment": "NSE_EQ",
            "securityId": "2885",
            "last_price": 100.5,
            "volume": 1000,
        })
        backend.feed_raw(raw)
        assert len(received) == 1
        assert received[0].ltp.value == Decimal("100.5")

    def test_dhan_segment(self) -> None:
        from tradex_brokers.dhan.client import dhan_segment

        assert dhan_segment(Equity.of("NSE", "X")) == "NSE_EQ"
        assert dhan_segment(Equity.of("BSE", "X")) == "BSE_EQ"
        assert dhan_segment(Index.of("NSE", "NIFTY")) == "IDX_I"
        assert dhan_segment(Future.of("NFO", "NIFTY", date(2024, 12, 26))) == "NSE_FNO"

    def test_row_to_quote(self) -> None:
        from tradex_brokers.common.ws_shared import row_to_quote

        inst = Equity.of("NSE", "RELIANCE")
        row = {
            "last_price": 100.5,
            "volume": 1000,
            "depth": {
                "buy": [{"price": 100, "quantity": 500}],
                "sell": [{"price": 101, "quantity": 300}],
            },
        }
        quote = row_to_quote(inst, row, provider="dhan")
        assert quote.ltp.value == Decimal("100.5")
        assert quote.volume.value == 1000
        assert quote.bid is not None
        assert quote.ask is not None
        assert quote.depth is not None

    def test_shape_dhan_quote_row(self) -> None:
        from tradex_brokers.dhan.ws_streams import _shape_dhan_quote_row

        payload = {"last_price": 100, "last_trade_time": "2024-01-01T00:00:00"}
        row = _shape_dhan_quote_row(payload)
        assert row["last_price"] == 100
        assert row["timestamp"] == "2024-01-01T00:00:00"


class TestUpstoxWsStreams:
    """Smoke tests for Upstox WS stream backends."""

    def test_upstox_portfolio_feed_raw(self) -> None:
        from tradex_brokers.upstox.ws_streams import UpstoxPortfolioStreamBackend

        received: list[Order] = []

        def map_order(row: Mapping[str, Any]) -> Order:
            return Order(
                order_id=OrderId(value="test"),
                instrument=Equity.of("NSE", "RELIANCE"),
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Quantity(value=Decimal("10")),
                price=None,
                time_in_force=TimeInForce.DAY,
                status=OrderStatus.ACK,
            )

        backend = UpstoxPortfolioStreamBackend(
            authorize_url="http://test",
            ws_fetch=lambda *a, **kw: (200, {}),
            token_provider=lambda: "token",
            map_order=map_order,
        )
        backend._order_handlers["test"] = received.append
        raw = json.dumps({"type": "order", "data": {"orderId": "123"}})
        backend.feed_raw(raw)
        assert len(received) == 1

    def test_upstox_portfolio_skips_non_order(self) -> None:
        from tradex_brokers.upstox.ws_streams import UpstoxPortfolioStreamBackend

        received: list[Order] = []

        def map_order(row: Mapping[str, Any]) -> Order:
            return Order(
                order_id=OrderId(value="test"),
                instrument=Equity.of("NSE", "RELIANCE"),
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Quantity(value=Decimal("10")),
                price=None,
                time_in_force=TimeInForce.DAY,
                status=OrderStatus.ACK,
            )

        backend = UpstoxPortfolioStreamBackend(
            authorize_url="http://test",
            ws_fetch=lambda *a, **kw: (200, {}),
            token_provider=lambda: "token",
            map_order=map_order,
        )
        backend._order_handlers["test"] = received.append
        raw = json.dumps({"type": "trade", "data": {}})
        backend.feed_raw(raw)
        assert len(received) == 0

    def test_instrument_from_id(self) -> None:
        from tradex_brokers.common.provider_common import instrument_from_id

        iid = InstrumentId.equity("NSE", "RELIANCE")
        inst = instrument_from_id(iid)
        assert isinstance(inst, Equity)

        fid = InstrumentId.future("NFO", "NIFTY", date(2024, 12, 26))
        inst = instrument_from_id(fid)
        assert isinstance(inst, Future)


# ---------------------------------------------------------------------------
# Task 5: from_fetch() factory methods
# ---------------------------------------------------------------------------


def _make_fetch_callable():
    """Return a fetch callable that returns (status, body) tuples."""
    def fetch(method, url, **kwargs):
        return (200, {"data": {"status": "ok"}})
    return fetch


class TestDhanApiClientFromFetch:
    """DhanApiClient.from_fetch() constructs a working client."""

    def test_from_fetch_returns_client(self):
        from tradex_brokers.dhan.client import DhanApiClient

        registry = InstrumentRegistry()
        client = DhanApiClient.from_fetch(
            fetch=_make_fetch_callable(),
            registry=registry,
            client_id="test-client",
        )
        assert isinstance(client, DhanApiClient)

    def test_from_fetch_with_access_token(self):
        from tradex_brokers.dhan.client import DhanApiClient

        registry = InstrumentRegistry()
        client = DhanApiClient.from_fetch(
            fetch=_make_fetch_callable(),
            registry=registry,
            access_token="tok-123",
        )
        assert isinstance(client, DhanApiClient)

    def test_from_fetch_with_token_manager(self):
        from unittest.mock import MagicMock

        from tradex_brokers.dhan.client import DhanApiClient

        registry = InstrumentRegistry()
        tm = MagicMock()
        tm.ensure_token.return_value = "managed-token"
        client = DhanApiClient.from_fetch(
            fetch=_make_fetch_callable(),
            registry=registry,
            token_manager=tm,
        )
        assert isinstance(client, DhanApiClient)


class TestDhanBrokerFromFetch:
    """DhanBroker.from_fetch() constructs a working broker."""

    def test_from_fetch_returns_broker(self):
        from tradex_brokers.dhan.adapter import DhanBroker

        broker = DhanBroker.from_fetch(
            fetch=_make_fetch_callable(),
            client_id="test-client",
        )
        assert isinstance(broker, DhanBroker)

    def test_from_fetch_stores_token_manager(self):
        from unittest.mock import MagicMock

        from tradex_brokers.dhan.adapter import DhanBroker

        tm = MagicMock()
        tm.ensure_token.return_value = "tok"
        broker = DhanBroker.from_fetch(
            fetch=_make_fetch_callable(),
            token_manager=tm,
        )
        assert broker._token_manager is tm  # type: ignore[attr-defined]

    def test_from_fetch_with_instrument_loader(self):
        from tradex_brokers.dhan.adapter import DhanBroker

        def loader():
            return [{"symbol": "RELIANCE", "exchange": "NSE"}]

        broker = DhanBroker.from_fetch(
            fetch=_make_fetch_callable(),
            instrument_loader=loader,
        )
        assert broker._instrument_loader is loader


class TestUpstoxApiClientFromFetch:
    """UpstoxApiClient.from_fetch() constructs a working client."""

    def test_from_fetch_returns_client(self):
        from tradex_brokers.upstox.client import UpstoxApiClient

        registry = InstrumentRegistry()
        client = UpstoxApiClient.from_fetch(
            fetch=_make_fetch_callable(),
            registry=registry,
        )
        assert isinstance(client, UpstoxApiClient)

    def test_from_fetch_with_all_base_urls(self):
        from tradex_brokers.upstox.client import UpstoxApiClient

        registry = InstrumentRegistry()
        client = UpstoxApiClient.from_fetch(
            fetch=_make_fetch_callable(),
            registry=registry,
            base_url="https://custom.api.com/v2",
            base_hft="https://custom-hft.api.com/v3",
            base_v3="https://custom.api.com/v3",
        )
        assert isinstance(client, UpstoxApiClient)

    def test_from_fetch_with_token_manager(self):
        from unittest.mock import MagicMock

        from tradex_brokers.upstox.client import UpstoxApiClient

        registry = InstrumentRegistry()
        tm = MagicMock()
        tm.ensure_token.return_value = "managed-token"
        client = UpstoxApiClient.from_fetch(
            fetch=_make_fetch_callable(),
            registry=registry,
            token_manager=tm,
        )
        assert isinstance(client, UpstoxApiClient)


class TestUpstoxBrokerFromFetch:
    """UpstoxBroker.from_fetch() constructs a working broker."""

    def test_from_fetch_returns_broker(self):
        from tradex_brokers.upstox.adapter import UpstoxBroker

        broker = UpstoxBroker.from_fetch(
            fetch=_make_fetch_callable(),
        )
        assert isinstance(broker, UpstoxBroker)

    def test_from_fetch_stores_token_manager(self):
        from unittest.mock import MagicMock

        from tradex_brokers.upstox.adapter import UpstoxBroker

        tm = MagicMock()
        tm.ensure_token.return_value = "tok"
        broker = UpstoxBroker.from_fetch(
            fetch=_make_fetch_callable(),
            token_manager=tm,
        )
        assert broker._token_manager is tm  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Task 5: Paper broker utilities
# ---------------------------------------------------------------------------


class TestPaperBrokerUtilities:
    """owns_position_projection, trading_cache, configure_runtime_cache."""

    def test_owns_position_projection_default(self):
        from tradex_brokers.paper.adapter import PaperBroker

        broker = PaperBroker()
        assert broker.owns_position_projection is True

    def test_owns_position_projection_disabled(self):
        from tradex_brokers.paper.adapter import PaperBroker

        broker = PaperBroker(project_positions=False)
        assert broker.owns_position_projection is False

    def test_trading_cache_exists(self):
        from tradex_brokers.paper.adapter import PaperBroker

        broker = PaperBroker()
        cache = broker.trading_cache
        # May be None if TradingCache import fails, or a TradingCache instance
        assert cache is None or hasattr(cache, "snapshot")

    def test_configure_runtime_cache_same_cache(self):
        from tradex_brokers.paper.adapter import PaperBroker

        broker = PaperBroker()
        if broker.trading_cache is None:
            return  # skip if TradingCache not available
        cache = broker.trading_cache
        broker.configure_runtime_cache(cache)
        assert broker.owns_position_projection is False

    def test_configure_runtime_cache_new_cache(self):
        from tradex_brokers.paper.adapter import PaperBroker

        broker = PaperBroker()
        if broker.trading_cache is None:
            return  # skip if TradingCache not available
        try:
            from tradex_trading.execution.trading_cache import TradingCache
        except ImportError:
            return
        new_cache = TradingCache()
        broker.configure_runtime_cache(new_cache)
        assert broker.trading_cache is new_cache
        assert broker.owns_position_projection is False
