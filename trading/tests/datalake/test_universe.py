"""Tests for universe loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from tradex_trading.datalake.universe import load_universe, available_universes


class TestLoadUniverse:
    def test_load_nifty50(self):
        instruments = load_universe("nifty50")
        assert len(instruments) > 0
        assert all(str(i.exchange) == "NSE" for i in instruments)

    def test_load_nifty500(self):
        instruments = load_universe("nifty500")
        assert len(instruments) > len(load_universe("nifty50"))

    def test_missing_universe_raises(self):
        with pytest.raises(FileNotFoundError, match="Universe CSV not found"):
            load_universe("nifty9999")

    def test_instruments_are_equity(self):
        from tradex_domain.instruments import Equity
        instruments = load_universe("nifty50")
        assert all(isinstance(i, Equity) for i in instruments)

    def test_reliance_in_nifty50(self):
        instruments = load_universe("nifty50")
        symbols = {str(i.instrument_id) for i in instruments}
        assert any("RELIANCE" in s for s in symbols)


class TestAvailableUniverses:
    def test_lists_available(self):
        universes = available_universes()
        assert "nifty50" in universes
        assert "nifty500" in universes

    def test_custom_dir(self, tmp_path):
        # Create a fake universe CSV
        csv_file = tmp_path / "nifty25_list.csv"
        csv_file.write_text("Company Name,Industry,Symbol,Series,ISIN Code\nTest Co,Test,TEST,EQ,INE001\n")
        universes = available_universes(csv_dir=tmp_path)
        assert "nifty25" in universes
