"""Tests for MCPDataLakeServer query handlers."""

from __future__ import annotations

import pytest

from tradex_trading.datalake.mcp_server import MCPDataLakeServer


class MockCatalog:
    def query_bars(self, symbol, start=None, end=None):
        return [{"timestamp": "2024-01-01", "open": "100", "close": "105"}]

    def list_tables(self):
        return ["NSE_RELIANCE", "NSE_TCS"]

    def get_schema(self):
        return ["timestamp", "open", "high", "low", "close", "volume"]

    def has_bars(self, symbol):
        return symbol in ["NSE_RELIANCE"]


@pytest.fixture()
def server():
    return MCPDataLakeServer(MockCatalog())


class TestQueryBars:
    def test_query_bars(self, server):
        result = server.query_bars("NSE_RELIANCE")
        assert result == [{"timestamp": "2024-01-01", "open": "100", "close": "105"}]


class TestListInstruments:
    def test_list_instruments(self, server):
        result = server.list_instruments()
        assert result == ["NSE_RELIANCE", "NSE_TCS"]


class TestGetSchema:
    def test_get_schema(self, server):
        result = server.get_schema()
        assert result == ["timestamp", "open", "high", "low", "close", "volume"]


class TestHasDataTrue:
    def test_has_data_true(self, server):
        assert server.has_data("NSE_RELIANCE") is True


class TestHasDataFalse:
    def test_has_data_false(self, server):
        assert server.has_data("NSE_UNKNOWN") is False
