"""Tests for DataEngine — unified data facade."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tradex_trading.datalake.data_engine import DataEngine


@pytest.fixture()
def mock_catalog():
    cat = MagicMock()
    cat.read_bars.return_value = [{"timestamp": "2024-01-01", "close": 100}]
    cat.list_instruments.return_value = [("NSE", "RELIANCE"), ("NSE", "TCS")]
    cat.has_bars.return_value = True
    cat.get_schema.return_value = ["timestamp", "open", "high", "low", "close", "volume"]
    return cat


@pytest.fixture()
def mock_quality_engine():
    qe = MagicMock()
    qe.check_gaps.return_value = []
    qe.check_duplicates.return_value = []
    qe.check_ohlc_integrity.return_value = []
    qe.check_volume.return_value = []
    return qe


@pytest.fixture()
def mock_source_policy():
    sp = MagicMock()
    sp.select.return_value = "DATALAKE"
    return sp


class TestGetBarsFromCatalog:
    def test_get_bars_from_catalog(self, mock_catalog):
        engine = DataEngine(mock_catalog)
        bars = engine.get_bars("RELIANCE", "1d")
        mock_catalog.read_bars.assert_called_once_with("RELIANCE", "1d", start=None, end=None)
        assert bars == [{"timestamp": "2024-01-01", "close": 100}]

    def test_get_bars_with_source_policy(self, mock_catalog, mock_source_policy):
        engine = DataEngine(mock_catalog, source_policy=mock_source_policy)
        engine.get_bars("RELIANCE", "1d", start="2024-01-01")
        mock_source_policy.select.assert_called_once_with("RELIANCE", "1d", "2024-01-01", None)


class TestValidateBars:
    def test_validate_bars_with_quality_engine(self, mock_catalog, mock_quality_engine):
        engine = DataEngine(mock_catalog, quality_engine=mock_quality_engine)
        bars = [MagicMock()]
        issues = engine.validate_bars(bars, timeframe="1d")
        mock_quality_engine.check_gaps.assert_called_once_with(bars, "1d")
        mock_quality_engine.check_duplicates.assert_called_once_with(bars)
        mock_quality_engine.check_ohlc_integrity.assert_called_once_with(bars)
        mock_quality_engine.check_volume.assert_called_once_with(bars)
        assert issues == []

    def test_validate_bars_without_quality_engine(self, mock_catalog):
        engine = DataEngine(mock_catalog)
        issues = engine.validate_bars([MagicMock()])
        assert issues == []


class TestListAvailableData:
    def test_list_available_data(self, mock_catalog):
        engine = DataEngine(mock_catalog)
        result = engine.list_available_data()
        mock_catalog.list_instruments.assert_called_once()
        assert result == [("NSE", "RELIANCE"), ("NSE", "TCS")]


class TestHasData:
    def test_has_data(self, mock_catalog):
        engine = DataEngine(mock_catalog)
        assert engine.has_data("RELIANCE") is True
        mock_catalog.has_bars.assert_called_once_with("RELIANCE")


class TestGetSchema:
    def test_get_schema(self, mock_catalog):
        engine = DataEngine(mock_catalog)
        schema = engine.get_schema()
        mock_catalog.get_schema.assert_called_once()
        assert schema == ["timestamp", "open", "high", "low", "close", "volume"]
