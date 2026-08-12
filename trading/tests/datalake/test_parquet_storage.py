"""Tests for ParquetStorage — Hive-partitioned Parquet store."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from tradex_trading.datalake.parquet_storage import ParquetStorage


def _frame(rows: list[dict]) -> pd.DataFrame:
    defaults = dict(symbol="RELIANCE", exchange="NSE", kind="equity",
                    timeframe="1m", volume=1000)
    for r in rows:
        for k, v in defaults.items():
            r.setdefault(k, v)
    return pd.DataFrame(rows)


class TestParquetStorage:
    def test_upsert_and_read(self, tmp_path):
        store = ParquetStorage(tmp_path)
        df = _frame([
            dict(timestamp="2026-07-01 09:15:00", open=100, high=101, low=99, close=100),
            dict(timestamp="2026-07-01 09:16:00", open=100, high=102, low=99, close=101),
        ])
        written = store.upsert(df)
        assert written == 2

        result = store.read(symbols=["RELIANCE"])
        assert len(result) == 2

    def test_upsert_is_idempotent(self, tmp_path):
        """Re-upserting same data replaces overlapping rows, not duplicates."""
        store = ParquetStorage(tmp_path)
        df = _frame([
            dict(timestamp="2026-07-01 09:15:00", open=100, high=101, low=99, close=100),
        ])
        store.upsert(df)
        store.upsert(df)  # same data again
        result = store.read(symbols=["RELIANCE"])
        assert len(result) == 1  # not 2

    def test_upsert_replaces_overlapping(self, tmp_path):
        """New data for same timestamp replaces old values."""
        store = ParquetStorage(tmp_path)
        df1 = _frame([dict(timestamp="2026-07-01 09:15:00", open=100, high=101, low=99, close=100)])
        df2 = _frame([dict(timestamp="2026-07-01 09:15:00", open=200, high=201, low=199, close=200)])
        store.upsert(df1)
        store.upsert(df2)
        result = store.read(symbols=["RELIANCE"])
        assert len(result) == 1
        assert float(result.iloc[0]["open"]) == 200.0

    def test_read_with_date_filter(self, tmp_path):
        store = ParquetStorage(tmp_path)
        df = _frame([
            dict(timestamp="2026-07-01 09:15:00", open=100, high=101, low=99, close=100),
            dict(timestamp="2026-08-01 09:15:00", open=200, high=201, low=199, close=200),
        ])
        store.upsert(df)
        result = store.read(symbols=["RELIANCE"],
                           start=datetime(2026, 7, 15), end=datetime(2026, 8, 15))
        assert len(result) == 1

    def test_symbols_list(self, tmp_path):
        store = ParquetStorage(tmp_path)
        df = _frame([dict(timestamp="2026-07-01 09:15:00", open=100, high=101, low=99, close=100)])
        store.upsert(df)
        assert "RELIANCE" in store.symbols()

    def test_empty_upsert_returns_zero(self, tmp_path):
        store = ParquetStorage(tmp_path)
        assert store.upsert(pd.DataFrame()) == 0

    def test_read_empty_store(self, tmp_path):
        store = ParquetStorage(tmp_path)
        result = store.read(symbols=["NONEXISTENT"])
        assert result.empty

    def test_clear(self, tmp_path):
        store = ParquetStorage(tmp_path)
        df = _frame([dict(timestamp="2026-07-01 09:15:00", open=100, high=101, low=99, close=100)])
        store.upsert(df)
        store.clear()
        assert store.read(symbols=["RELIANCE"]).empty

    def test_multiple_symbols(self, tmp_path):
        store = ParquetStorage(tmp_path)
        df1 = _frame([dict(timestamp="2026-07-01 09:15:00", open=100, high=101, low=99, close=100)])
        df2 = _frame([dict(timestamp="2026-07-01 09:15:00", open=200, high=201, low=199, close=200)])
        df2["symbol"] = "TCS"
        store.upsert(pd.concat([df1, df2], ignore_index=True))
        syms = store.symbols()
        assert "RELIANCE" in syms
        assert "TCS" in syms

    def test_partition_pruning(self, tmp_path):
        """Read only scans the months that overlap the date range."""
        store = ParquetStorage(tmp_path)
        # Data spanning 3 months
        rows = []
        for month in [6, 7, 8]:
            rows.append(dict(timestamp=f"2026-{month:02d}-15 09:15:00",
                            open=100, high=101, low=99, close=100))
        store.upsert(_frame(rows))
        # Read only July
        result = store.read(symbols=["RELIANCE"],
                           start=datetime(2026, 7, 1), end=datetime(2026, 7, 31))
        assert len(result) == 1
