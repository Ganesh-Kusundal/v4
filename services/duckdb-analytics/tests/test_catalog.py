"""Catalog tests — views, session strip, metadata."""

from __future__ import annotations

import pandas as pd


class TestViews:
    def test_session_strip_excludes_post_market(self, service):
        raw = service.execute("SELECT count(*) AS n FROM ohlcv_raw").rows[0][0]
        stripped = service.execute("SELECT count(*) AS n FROM ohlcv").rows[0][0]
        assert raw == stripped + 1  # exactly the phantom 18:00 bar removed

    def test_phantom_bar_values_absent_from_ohlcv(self, service):
        rows = service.execute(
            "SELECT close FROM ohlcv WHERE close = 999.0"
        ).rows
        assert rows == []
        assert len(service.execute(
            "SELECT close FROM ohlcv_raw WHERE close = 999.0"
        ).rows) == 1

    def test_hive_columns_extracted(self, catalog):
        cols = dict(catalog.schema_of("ohlcv"))
        assert cols["symbol"] != "" and cols["year"] != "" and cols["month"] != ""


class TestMeta:
    def test_list_symbols_sorted(self, catalog):
        assert catalog.list_symbols() == ["RELIANCE", "TCS"]

    def test_date_range(self, catalog):
        lo, hi = catalog.date_range("RELIANCE")
        assert (lo.year, lo.month, lo.day) == (2026, 8, 5)
        # raw view: spans the whole fixture window (incl. phantom bar day)
        assert hi.date() == pd.Timestamp("2026-08-12").date()

    def test_date_range_unknown_symbol(self, catalog):
        assert catalog.date_range("NOPE") is None

    def test_schema_rejects_unknown_view(self, catalog):
        import pytest
        with pytest.raises(ValueError):
            catalog.schema_of("secret")
