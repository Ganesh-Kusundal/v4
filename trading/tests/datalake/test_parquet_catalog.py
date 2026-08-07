"""Tests for ParquetDataCatalog."""

from __future__ import annotations

import pytest

from tradex_trading.datalake.parquet_catalog import ParquetDataCatalog


class TestParquetDataCatalogInstantiation:
    def test_creates_root_directory(self, tmp_path):
        root = tmp_path / "catalog"
        ParquetDataCatalog(root)
        assert root.exists()

    def test_accepts_string_path(self, tmp_path):
        catalog = ParquetDataCatalog(str(tmp_path / "catalog"))
        assert catalog._root.exists()


class TestHasBars:
    def test_returns_false_for_missing(self, tmp_path):
        catalog = ParquetDataCatalog(tmp_path)
        assert catalog.has_bars("NSE:RELIANCE", "1m") is False

    def test_returns_false_for_wrong_timeframe(self, tmp_path):
        catalog = ParquetDataCatalog(tmp_path)
        assert catalog.has_bars("NSE:RELIANCE", "5m") is False


class TestListTables:
    def test_empty_directory(self, tmp_path):
        catalog = ParquetDataCatalog(tmp_path)
        assert catalog.list_tables() == []


pa = pytest.importorskip("pyarrow", reason="pyarrow not installed")


class TestWriteAndReadBars:
    def test_roundtrip(self, tmp_path):
        catalog = ParquetDataCatalog(tmp_path)
        bars = [
            {
                "timestamp": "2026-01-01T09:15:00",
                "open": 100.0, "high": 105.0,
                "low": 99.0, "close": 103.0,
                "volume": 1000,
            },
            {
                "timestamp": "2026-01-01T09:16:00",
                "open": 103.0, "high": 107.0,
                "low": 102.0, "close": 106.0,
                "volume": 1500,
            },
        ]
        catalog.write_bars("NSE:RELIANCE", "1m", bars)
        assert catalog.has_bars("NSE:RELIANCE", "1m")
        result = catalog.read_bars("NSE:RELIANCE", "1m")
        assert len(result["timestamp"]) == 2

    def test_read_missing_returns_empty(self, tmp_path):
        catalog = ParquetDataCatalog(tmp_path)
        assert catalog.read_bars("NSE:RELIANCE", "1m") == []

    def test_write_empty_bars_is_noop(self, tmp_path):
        catalog = ParquetDataCatalog(tmp_path)
        catalog.write_bars("NSE:RELIANCE", "1m", [])
        assert catalog.has_bars("NSE:RELIANCE", "1m") is False

    def test_list_tables_after_write(self, tmp_path):
        catalog = ParquetDataCatalog(tmp_path)
        bar = {"timestamp": "t", "open": 1, "high": 2, "low": 0, "close": 1, "volume": 0}
        catalog.write_bars("NSE:RELIANCE", "1m", [bar])
        tables = catalog.list_tables()
        assert len(tables) == 1
        assert "NSE_RELIANCE_1m" in tables[0]


class TestGetSchema:
    def test_returns_schema(self, tmp_path):
        catalog = ParquetDataCatalog(tmp_path)
        bar = {"timestamp": "t", "open": 1, "high": 2, "low": 0, "close": 1, "volume": 0}
        catalog.write_bars("NSE:RELIANCE", "1m", [bar])
        schema = catalog.get_schema("NSE:RELIANCE", "1m")
        assert schema is not None
        assert "timestamp" in schema
        assert "close" in schema

    def test_returns_none_for_missing(self, tmp_path):
        catalog = ParquetDataCatalog(tmp_path)
        assert catalog.get_schema("NSE:RELIANCE", "1m") is None


duckdb_mod = pytest.importorskip("duckdb", reason="duckdb not installed")


class TestQuery:
    def test_query_returns_rows(self, tmp_path):
        catalog = ParquetDataCatalog(tmp_path)
        bar = {
            "timestamp": "2026-01-01", "open": 100, "high": 105,
            "low": 99, "close": 103, "volume": 1000,
        }
        catalog.write_bars("NSE:RELIANCE", "1m", [bar])
        result = catalog.query("SELECT 1 AS x")
        assert result == [(1,)]
