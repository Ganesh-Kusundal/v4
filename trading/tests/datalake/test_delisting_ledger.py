"""Tests for DataCatalog delisting ledger (Phase 5.4)."""

from __future__ import annotations

from pathlib import Path

from tradex_trading.datalake.catalog import DataCatalog


class TestDelistingLedger:
    """Survivorship-bias protection via delisting ledger."""

    def test_mark_and_check_delisted(self, tmp_path: Path) -> None:
        catalog = DataCatalog(root=tmp_path)
        assert not catalog.is_delisted("RELIANCE")
        catalog.mark_delisted("RELIANCE", "2026-08-01", reason="merger")
        assert catalog.is_delisted("RELIANCE")

    def test_delisted_instruments_returns_ledger(self, tmp_path: Path) -> None:
        catalog = DataCatalog(root=tmp_path)
        catalog.mark_delisted("RELIANCE", "2026-08-01", reason="merger")
        catalog.mark_delisted("TCS", "2026-07-15", reason="delisting")
        ledger = catalog.delisted_instruments()
        assert "RELIANCE" in ledger
        assert ledger["RELIANCE"]["delisted_on"] == "2026-08-01"
        assert ledger["RELIANCE"]["reason"] == "merger"
        assert "TCS" in ledger

    def test_active_symbols_excludes_delisted(self, tmp_path: Path) -> None:
        catalog = DataCatalog(root=tmp_path)
        catalog.write_bars("RELIANCE", [
            {
                "timestamp": "2026-01-01T00:00:00",
                "open": "100",
                "high": "101",
                "low": "99",
                "close": "100",
                "volume": "1000",
            },
        ])
        catalog.write_bars("TCS", [
            {
                "timestamp": "2026-01-01T00:00:00",
                "open": "200",
                "high": "201",
                "low": "199",
                "close": "200",
                "volume": "500",
            },
        ])
        assert set(catalog.active_symbols()) == {"RELIANCE", "TCS"}
        catalog.mark_delisted("RELIANCE", "2026-08-01")
        assert catalog.active_symbols() == ["TCS"]

    def test_historical_bars_still_readable_after_delisting(self, tmp_path: Path) -> None:
        catalog = DataCatalog(root=tmp_path)
        catalog.write_bars("RELIANCE", [
            {
                "timestamp": "2026-01-01T00:00:00",
                "open": "100",
                "high": "101",
                "low": "99",
                "close": "100",
                "volume": "1000",
            },
        ])
        catalog.mark_delisted("RELIANCE", "2026-08-01")
        bars = catalog.query_bars("RELIANCE")
        assert len(bars) == 1
