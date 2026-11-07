"""Regression tests for the market-data routes that were broken at HEAD."""

from __future__ import annotations

import asyncio
from decimal import Decimal

from tradex_domain.instruments import Equity
from tradex_domain.market import Quote
from tradex_domain.value_objects import Price
from tradex_interfaces.routes import market_data


class _Broker:
    def get_quote(self, instrument):
        return Quote(instrument=instrument, ltp=Price(Decimal("100.5")))

    def search(self, query):
        return [Equity.of("NSE", "RELIANCE")]


class _Session:
    broker = _Broker()


def test_quote_handler_serializes_a_quote():
    result = asyncio.run(market_data.get_quote("NSE", "RELIANCE", _Session()))
    assert isinstance(result, dict)
    assert "instrument" in result
    assert "ltp" in result


def test_search_handler_returns_metadata_dicts():
    result = asyncio.run(market_data.search_instruments("REL", _Session()))
    assert result == [
        {
            "instrument_id": "NSE:RELIANCE",
            "symbol": "RELIANCE",
            "exchange": "NSE",
            "asset_class": "equity",
        }
    ]


def test_stream_module_imports():
    import tradex_interfaces.routes.stream as stream

    assert callable(stream.ws_stream)
