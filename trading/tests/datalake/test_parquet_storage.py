"""Tests for ParquetStorage — Hive-partitioned Parquet store."""

from __future__ import annotations

from datetime import datetime, time

import pandas as pd

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
        df2 = _frame([dict(timestamp="2026-07-01 09:15:00",
                           open=200, high=201, low=199, close=200)])
        store.upsert(df1)
        store.upsert(df2)
        result = store.read(symbols=["RELIANCE"])
        assert len(result) == 1
        assert float(result.iloc[0]["open"]) == 200.0

    def test_read_with_date_filter(self, tmp_path):
        store = ParquetStorage(tmp_path)
        df = _frame([
            dict(timestamp="2026-07-01 09:15:00", open=100, high=101, low=99, close=100),
            dict(timestamp="2026-08-03 09:15:00", open=200, high=201, low=199, close=200),
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

    def test_upsert_drops_phantom_session_rows(self, tmp_path):
        """Write path strips non-NSE-session bars: weekend + pre/post market.

        Regression for the 2026-02-01 phantom Sunday session (~187k rows)
        the old path stored because read-time strip was time-of-day only.
        """
        store = ParquetStorage(tmp_path)
        df = _frame([
            # Sunday full session — must be dropped
            dict(timestamp="2026-02-01 09:15:00", open=100, high=101, low=99, close=100),
            dict(timestamp="2026-02-01 15:29:00", open=100, high=101, low=99, close=100),
            # pre-market / post-market — dropped
            dict(timestamp="2026-07-10 03:45:00", open=100, high=101, low=99, close=100),
            dict(timestamp="2026-07-10 16:00:00", open=100, high=101, low=99, close=100),
            # a genuine weekday-session bar — kept
            dict(timestamp="2026-07-10 10:00:00", open=100, high=101, low=99, close=100),
        ])
        written = store.upsert(df)
        assert written == 1  # only the real session bar

        result = store.read(symbols=["RELIANCE"], strip_post_market=False)
        assert len(result) == 1
        assert result.iloc[0]["timestamp"].hour == 10

    def test_read_strips_weekend_and_offsession(self, tmp_path):
        """read(strip_post_market=True) excludes weekends + pre/post-market."""
        store = ParquetStorage(tmp_path)
        df = _frame([
            dict(timestamp="2026-02-01 10:00:00", open=100, high=101, low=99, close=100),  # Sun
            dict(timestamp="2026-02-03 10:00:00", open=100, high=101, low=99, close=100),  # Tue
            dict(timestamp="2026-02-03 08:00:00", open=100, high=101, low=99, close=100),  # pre
        ])
        store.upsert(df)  # write guard already drops all but the Tue bar
        # Write raw phantom rows around the guard to exercise read-stripping:
        p = tmp_path / "ohlcv" / "symbol=RELIANCE" / "year=2026" / "month=02" / "data.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        raw = _frame([
            dict(timestamp="2026-02-01 10:00:00", open=1, high=2, low=0.5, close=1.5),  # Sun
        ])
        raw["timestamp"] = pd.to_datetime(raw["timestamp"])
        store._write_parquet(raw, p)
        all_rows = store.read(symbols=["RELIANCE"], strip_post_market=False)
        stripped = store.read(symbols=["RELIANCE"], strip_post_market=True)
        assert all_rows["timestamp"].dt.dayofweek.eq(6).any()   # Sunday present raw
        assert len(stripped) > 0
        assert not stripped["timestamp"].dt.dayofweek.eq(6).any()  # stripped on read
        assert stripped["timestamp"].dt.time.between(
            time(9, 15), time(15, 30)).all()

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
        df2 = _frame([dict(timestamp="2026-07-01 09:15:00",
                           open=200, high=201, low=199, close=200)])
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

    def test_read_strips_post_market_bars_by_default(self, tmp_path):
        """Bars outside 09:15-15:30 IST are excluded from read() by default."""
        store = ParquetStorage(tmp_path)
        df = _frame([
            dict(timestamp="2026-07-01 09:15:00", open=100, high=101, low=99, close=100),
            dict(timestamp="2026-07-01 17:00:00", open=100, high=101, low=99, close=100),
        ])
        store.upsert(df)
        result = store.read(symbols=["RELIANCE"])
        assert len(result) == 1
        assert result["timestamp"].dt.time.max() <= time(15, 30)

    def test_read_keeps_post_market_bars_when_disabled(self, tmp_path):
        """strip_post_market=False returns the raw stored bars.

        Uses ``_write_parquet`` to place phantom bars directly (the write
        guard now strips them on upsert), isolating read-flag behavior.
        """
        store = ParquetStorage(tmp_path)
        p = tmp_path / "ohlcv" / "symbol=RELIANCE" / "year=2026" / "month=07" / "data.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        raw = _frame([
            dict(timestamp="2026-07-01 09:15:00", open=100, high=101, low=99, close=100),
            dict(timestamp="2026-07-01 17:00:00", open=100, high=101, low=99, close=100),
        ])
        raw["timestamp"] = pd.to_datetime(raw["timestamp"])
        store._write_parquet(raw, p)
        result = store.read(symbols=["RELIANCE"], strip_post_market=False)
        assert len(result) == 2
