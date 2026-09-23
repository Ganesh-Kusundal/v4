"""Tests for ParquetMarketProvider — datalake-backed market provider."""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
from tradex_domain.enums import Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.market import HistoricalSeries

from tradex_trading.datalake.market_provider import ParquetMarketProvider
from tradex_trading.datalake.parquet_storage import ParquetStorage


def _upsert_minute_bars(store: ParquetStorage, symbol: str = "RELIANCE") -> None:
    """Write one 1m bar per minute across two days."""
    rows = []
    for day in (1, 2):
        for minute in range(9 * 60 + 15, 9 * 60 + 16):
            ts = f"2026-07-{day:02d} 09:{minute % 60:02d}:00"
            rows.append(
                dict(symbol=symbol, exchange="NSE", kind="equity", timeframe="1m",
                     timestamp=ts, open=100, high=102, low=99, close=101, volume=1000)
            )
    store.upsert(pd.DataFrame(rows))


class TestParquetMarketProvider:
    def test_history_preserves_requested_metadata_and_inclusive_bounds(self, tmp_path):
        store = ParquetStorage(tmp_path)
        _upsert_minute_bars(store)
        provider = ParquetMarketProvider(store=store)
        start = datetime(2026, 7, 1, 9, 15)
        end = datetime(2026, 7, 2, 9, 15)

        series = provider.history(Equity.of("NSE", "RELIANCE"), Timeframe.M1, start, end)

        assert series.instrument.symbol == "RELIANCE"
        assert series.timeframe == Timeframe.M1
        assert series.start == start
        assert series.end == end
        assert [c.timestamp for c in series.candles] == [
            datetime(2026, 7, 1, 9, 15),
            datetime(2026, 7, 2, 9, 15),
        ]

    def test_history_normalizes_timezone_aware_window_without_changing_metadata(self, tmp_path):
        store = ParquetStorage(tmp_path)
        _upsert_minute_bars(store)
        provider = ParquetMarketProvider(store=store)
        start = datetime(2026, 7, 1, 9, 15, tzinfo=UTC)
        end = datetime(2026, 7, 2, 9, 15, tzinfo=UTC)

        series = provider.history(Equity.of("NSE", "RELIANCE"), Timeframe.M1, start, end)

        assert len(series.candles) == 2
        assert series.start == start
        assert series.end == end

    def test_history_returns_m1_candles(self, tmp_path):
        store = ParquetStorage(tmp_path)
        _upsert_minute_bars(store)
        provider = ParquetMarketProvider(store=store)

        inst = Equity.of("NSE", "RELIANCE")
        series = provider.history(
            inst, Timeframe.M1,
            datetime(2026, 7, 1), datetime(2026, 7, 31),
        )
        assert isinstance(series, HistoricalSeries)
        assert len(series.candles) == 2
        assert all(c.timeframe == Timeframe.M1 for c in series.candles)
        assert float(series.candles[0].ohlc.close.value) == 101.0

    def test_history_resamples_to_d1(self, tmp_path):
        store = ParquetStorage(tmp_path)
        _upsert_minute_bars(store)
        provider = ParquetMarketProvider(store=store)

        inst = Equity.of("NSE", "RELIANCE")
        series = provider.history(
            inst, Timeframe.D1,
            datetime(2026, 7, 1), datetime(2026, 7, 31),
        )
        assert series.timeframe == Timeframe.D1
        assert len(series.candles) == 2  # one per day

    def test_history_missing_symbol_returns_empty(self, tmp_path):
        store = ParquetStorage(tmp_path)
        _upsert_minute_bars(store)
        provider = ParquetMarketProvider(store=store)

        inst = Equity.of("NSE", "NONEXISTENT")
        series = provider.history(
            inst, Timeframe.D1,
            datetime(2026, 7, 1), datetime(2026, 7, 31),
        )
        assert isinstance(series, HistoricalSeries)
        assert series.candles == []

    def test_history_empty_store(self, tmp_path):
        store = ParquetStorage(tmp_path)
        provider = ParquetMarketProvider(store=store)
        inst = Equity.of("NSE", "RELIANCE")
        series = provider.history(inst, Timeframe.M1, datetime(2026, 7, 1), datetime(2026, 7, 31))
        assert series.candles == []

    def test_store_property_exposes_underlying_store(self, tmp_path):
        store = ParquetStorage(tmp_path)
        _upsert_minute_bars(store)
        provider = ParquetMarketProvider(store=store)
        assert "RELIANCE" in provider.store.symbols()

    def test_default_base_path_constructs(self, tmp_path):
        """Constructing with an explicit base path wires the store (no repo side effects)."""
        provider = ParquetMarketProvider(base_path=str(tmp_path / "nested" / "data"))
        assert provider.store is not None
        # store resolves base_path/ohlcv
        assert (tmp_path / "nested" / "data" / "ohlcv").is_dir()
